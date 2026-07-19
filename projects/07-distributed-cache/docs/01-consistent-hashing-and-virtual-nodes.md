# Consistent Hashing and Virtual Nodes — From First Principles

> What this teaches: how a cluster decides *which node owns a key* with no
> coordinator, why the obvious answer (`hash % N`) destroys the cache every time
> the cluster resizes, and how a hash **ring** plus **virtual nodes** fixes both
> the movement problem and the balance problem. No prior knowledge assumed.
> Prepares you for **V2** in [SPEC.md](../SPEC.md) — the ring you'll build in
> [ring.rs](../src/ring.rs), where `add_node`, `remove_node`, `replicas`, and
> `node_count` are currently `todo!()`.
>
> Every number in this doc was computed with this project's *actual* hash
> function (`ring_position` in [ring.rs](../src/ring.rs): SHA-256, leading
> 8 bytes as a big-endian `u64`) — you can reproduce them.

---

## The one sentence to hold onto

**Hash the *nodes* onto the same circle as the keys, and membership changes stop
being global renumberings: a node's arrival or departure only touches the arc it
owns, so ≈1/N of keys move instead of ≈all of them.**

---

## 1. The problem: sharding is easy, *resharding* is the trap

One node can't hold the working set, so you split the keyspace across N nodes.
The obvious scheme:

```
owner(key) = hash(key) % N
```

Deterministic, uniform, O(1), no state. Everyone invents it, it works — right up
until N changes. Then look what happens. Take a key `k` and follow its owner as
the cluster grows from 4 nodes to 5:

```
owner = hash(k) % 4        owner = hash(k) % 5
```

`% 4` and `% 5` agree only when `hash(k) mod 20 ∈ {0,1,2,3}` — 4 cases out of
20. **80% of all keys change owner** because you added *one* node. Measured with
this project's hash over 200,000 keys: **79.9% moved** going 4→5, and **90.0%**
going 9→10. The general rule for `% N → % (N+1)` is that only ~`1/(N+1)` of keys
*stay* — the bigger your cluster, the worse the flush.

Why that's an outage and not an inconvenience:

| What a moved key means for a cache | Consequence |
|---|---|
| Requests for it now route to a node that has never seen it | **cold miss** |
| 80–90% of the entire keyspace does this simultaneously | hit ratio falls off a cliff cluster-wide |
| Every miss falls through to the database the cache was protecting | the database absorbs ~the full read load at once |
| And you did this by *adding capacity* | scaling up caused the outage — on Black Friday, per the SPEC's framing |

That's the V2 criterion in one line: adding a node must remap **≈1/N** of keys —
the keys the new node should own — *not* ≈all of them.

## 2. The fix: put nodes and keys on the same circle

The trick is to stop making ownership depend on N. Instead:

1. Treat the hash output space (here, all `u64` values, `0` to `2⁶⁴−1`) as a
   **circle** — after the max value you wrap to 0.
2. Hash **each node** (by its stable `NodeId` — see [node.rs](../src/node.rs)
   for why the id must survive restarts) to a position on that circle.
3. Hash **each key** to a position the same way.
4. **A key belongs to the first node you meet walking clockwise** from the key's
   position.

Real positions, from this project's hash function (shown as % of the way around
the ring):

| Hashed string | Position (% around ring) |
|---|---|
| `cache-b#0` | 15.0% |
| `cache-c#0` | 36.9% |
| `cache-a#0` | 48.4% |
| key `hello` | 17.6% |
| key `session:9` | 66.6% |
| key `user:42` | 91.5% |

```
                0%/100%
                   │
        user:42 ●  │   ● hello (17.6%) ──clockwise──▶ cache-b? no, passed it
        (91.5%)    │   ▼
             ┌─────┴─────┐ ● cache-b (15.0%)
             │           │
   wraps to  │  the ring │ ● cache-c (36.9%)  ◀── hello's owner
   cache-b ◀─┤ (all u64) │
             │           │ ● cache-a (48.4%)
             └─────┬─────┘
                   │  ● session:9 (66.6%) ──clockwise──▶ nothing until wrap
                   │                                      → cache-b owns it
```

Walking clockwise ("first node position ≥ my position, wrapping to the smallest
if none"): `hello` (17.6%) → **cache-c**; `session:9` (66.6%) → past cache-a,
wraps → **cache-b**; `user:42` (91.5%) → wraps → **cache-b**.

**Why this bounds movement.** Each node owns exactly the *arc* between its
predecessor and itself. Add a new node `cache-d`: it lands at one position and
claims only the arc between it and the node before it — every key outside that
arc still walks clockwise to the same owner as before. Remove a node: only *its*
arc's keys move (to the next node clockwise). Measured with this project's hash,
3 nodes → 4 (128 vnodes each): **27.6% of 60,000 keys moved** — right at the
theoretical 1/4, versus ~80% for `% N`. Ownership stopped depending on N; it
depends only on *where you landed*.

The V2 criteria this section maps to: determinism (same key + same membership →
same owner), and minimal disruption (the ≈1/N test the SPEC's Proof demands —
your test must assert ≈1/N, *not* ≈1).

## 3. The new problem the ring creates: clumping

Random positions on a circle are *not* evenly spaced — with a handful of nodes,
arc sizes vary wildly. This is not hypothetical; it's this project's own hash
with one position per node, 60,000 keys, 3 nodes:

| | cache-a | cache-b | cache-c | max/min |
|---|---|---|---|---|
| Keys owned (1 vnode each) | 6,933 (11.6%) | **39,926 (66.5%)** | 13,141 (21.9%) | **5.76×** |

`cache-b` owns two-thirds of the keyspace purely because of where three hashes
happened to land (it inherits the giant arc from 48.4% around to 15.0%). A
"balanced" cluster where one node does 5.8× the work of another will OOM or melt
that node first.

## 4. Virtual nodes: buy balance with ring memory

The fix is statistical: give each physical node **many** positions instead of
one. Hash `"cache-a#0"`, `"cache-a#1"`, … `"cache-a#127"` (the `#i` suffix is
exactly what the `add_node` TODO in [ring.rs](../src/ring.rs) describes) —
each is a **virtual node (vnode)** pointing back at the same physical node. A
node's total ownership becomes the *sum of many small arcs* scattered around the
ring, and sums of many random pieces concentrate near the mean (law of large
numbers). Same 3 nodes and 60,000 keys, varying the vnode count:

| vnodes/node | min keys | max keys | max/min |
|---|---|---|---|
| 1 | 6,933 | 39,926 | 5.76× |
| 8 | 17,424 | 23,822 | 1.37× |
| 32 | 19,211 | 20,498 | 1.07× |
| 128 | 19,506 | 20,422 | **1.05×** |

That's the V2 criterion "increasing the vnode count measurably flattens the load
distribution" — your Proof test asserts this shrinking spread. This project's
compose file ships `VNODES_PER_NODE: 128`.

**What vnodes cost** (the tradeoff `docs/07-design.md` asks you to record): the
ring holds `nodes × vnodes` entries, and every membership change inserts or
removes that many positions. 128 vnodes × 100 nodes = 12,800 ring entries —
trivial memory, but it's why lookup must be **sub-linear** (V2's last
criterion): a linear scan of all vnodes on every request would make the router
O(nodes × vnodes). The scaffold's TODO names the classic shape — a sorted
structure searched for "first position ≥ hash(key)", wrapping past the top —
and leaves the choice and the wrapping mechanics to you.

Two bonus properties fall out of vnodes (both CONCEPTS.md depth probes):

- **Weighted nodes**: a box with 2× the RAM just gets 2× the vnodes — capacity
  proportional to vnode count, no new mechanism.
- **Rebalance granularity**: when a node dies, its 128 arcs are scattered, so its
  load spreads over *all* survivors rather than dumping wholesale onto one
  clockwise neighbor.

## 5. Replica sets: walk clockwise, skip duplicates

V4 will store each key on N nodes. The ring gives you the replica set for free:
from the key's position, keep walking clockwise and collect nodes. In the
1-vnode picture above, `replicas("hello", 2)` walks from 17.6%: cache-c (36.9%),
then cache-a (48.4%) → `[cache-c, cache-a]`.

But with vnodes there's a trap the V2 criterion calls out explicitly: consecutive
ring positions are frequently vnodes of the **same physical node**. If
`replicas("hello", 2)` returned `[cache-c, cache-c]`, your "2 replicas" live on
one machine — one power cut deletes both copies, which defeats the entire point
of replication. So the walk must collect **distinct physical** nodes, skipping
vnodes whose owner is already in the set, and stop early if `n` exceeds the
number of physical nodes (`Ring::replicas` in [ring.rs](../src/ring.rs) spells
out this contract; `owner` is already implemented as `replicas(key, 1)`).

## 6. Two things the ring does *not* do

Worth internalizing before you over-trust it:

1. **It doesn't move data.** The ring changes who *should* own a key; nobody
   copies the bytes to the new owner. In this project a remapped key simply
   **cold-misses** on its new node and repopulates on the next write — a fine
   choice *for a cache* (the SPEC's caching horizontal asks you to document
   exactly this rebalance semantic). A database using the same ring must instead
   migrate data, which is enormously harder — this is a big part of why caches
   get to be simple.
2. **It balances *keyspace*, not *load*.** Equal key counts ≠ equal traffic. One
   viral key still sends all of its traffic to one node — CONCEPTS.md's Card 2
   trap. Hot-key handling (client-side L1, replicating hot reads) is a separate
   problem that the ring cannot solve.

And one design footnote from the scaffold worth understanding: `ring_position`
uses **SHA-256** rather than Rust's `DefaultHasher` because the ring must be
identical *across machines and restarts* — `DefaultHasher` is randomly seeded
per process, which would give every node a different ring (and every restart a
full reshuffle). Distribution quality matters here, not attack resistance; a
crypto hash is overkill that happens to be already in the workspace. Record your
hash + vnode choice in `docs/07-design.md` — it's one of the four graded
decisions.

## 7. Mental model summary

| Question | Answer to hold onto |
|---|---|
| Why is `% N` catastrophic? | Ownership depends on N itself; N→N+1 remaps ~N/(N+1) of keys (measured: 80% at N=4) — a cluster-wide cold-miss storm caused by scaling |
| Why does the ring bound movement? | Ownership depends on *position*, not count; a join/leave only touches one node's arcs — measured 27.6% ≈ 1/4 for 3→4 |
| What do vnodes fix? | Arc-size variance → load variance (5.76× imbalance at 1 vnode → 1.05× at 128, measured) |
| What do vnodes cost? | Ring memory and churn per membership change — hence sub-linear lookup is required |
| Why dedupe replicas? | Adjacent vnodes often share a physical node; "N replicas" on one box is zero fault tolerance |
| What happens to the data when ownership moves? | Nothing — a cache chooses cold-miss over migration, and documents it |

## 8. Where you'll build this

Everything lands in [ring.rs](../src/ring.rs) — a pure data structure, no async,
no locks (membership wraps it later), which is what makes the V2 proofs directly
unit-testable:

- the ring state inside `Ring` (the `TODO(V2)` marks the spot),
- `add_node` (idempotent — re-adding must not duplicate positions),
  `remove_node`, `replicas` (the distinct-physical clockwise walk), `node_count`,
- the tests sketched at the bottom: determinism, ≈1/(N+1) movement (**not** ≈1),
  vnode-count-flattens-spread, and distinct-replicas.

This doc unlocks the five **Done when ALL true** boxes of **V2** in
[SPEC.md](../SPEC.md), and feeds two later stages: V3 wires membership changes
into `add_node`/`remove_node` (see the TODO in
[membership.rs](../src/membership.rs)), and V4's coordinator reads
`replicas(key, n)` to route. The decisions you own: the sorted structure and its
wrap-around, the vnode count, and the hash — all recorded in `docs/07-design.md`.
For the build itself, `/hint` and `/quest` take over where this doc stops.
