# Replication, Quorums, and Choosing Availability — From First Principles

> What this teaches: why sharding alone turns every node death into data loss,
> how replicating each key to N ring successors fixes that, what a *coordinator*
> is and why any node can be one, and how N, R, and W are three separate dials
> whose setting is a consistency *choice you make on purpose*. No prior knowledge
> assumed. Prepares you for **V4** in [SPEC.md](../SPEC.md) — the routing brain
> you'll build in [coordinator.rs](../src/coordinator.rs), where `get`, `put`,
> and `delete` are `todo!()` (the local operations and the replica-resolution
> helper are already wired).

---

## The one sentence to hold onto

**Store every key on the next N distinct nodes around the ring and let any node
coordinate any request — then N (copies), W (acks before a write returns), and R
(replicas consulted per read) become three dials, and where you set them is you
choosing, out loud, how stale a read you'll tolerate in exchange for speed and
availability.**

---

## 1. The problem: a shard is a single point of failure

V2+V3 give you a cluster where each key lives on exactly one node, and the ring
retargets ownership when a node dies. Notice what that *doesn't* do: the dead
node's data is gone. The ring says "cache-c's keys now belong to cache-b" — but
cache-b has never seen those keys. With `CACHE_CAPACITY: 100000` per node, one
node death means up to 100,000 keys vanish *simultaneously*.

For a cache, "data loss" has a specific cost profile:

| Event | Consequence |
|---|---|
| One node dies | its entire shard cold-misses at once |
| Every miss falls through | a synchronized read burst into the database the cache was shielding |
| Which arrives correlated | not spread over hours like organic misses — one spike, right when you already have a failing node |

The V4 headline criterion says it plainly: killing **one** replica of a key
leaves the value still `GET`-able — a single node loss is **not** data loss for
replicated keys.

## 2. Replication: the ring already told you where the copies go

V2 built the answer in advance. `Ring::replicas(key, n)` walks clockwise from
the key's hash collecting the first n **distinct physical** nodes. Replication
is just: *store the value on all of them*.

Using the real ring positions from
[doc 01](01-consistent-hashing-and-virtual-nodes.md) (1 vnode each, N=2 — the
compose file's `REPLICATION_FACTOR: 2`):

```
key "hello" hashes to 17.6% around the ring
walk clockwise:  cache-c (36.9%)  ← replica 1 (the "primary" successor)
                 cache-a (48.4%)  ← replica 2
```

`PUT /cache/hello` must land the bytes on cache-c *and* cache-a. Now
`docker compose kill cache-c` and `GET /cache/hello` still works — the ring
(post-SWIM-detection) resolves to survivors, and cache-a has the copy. The
distinct-physical-nodes rule from V2 is what makes this real fault tolerance:
two vnodes of the same box would be zero protection.

## 3. Coordination: any node can take any request

Clients shouldn't need to know the topology (which changes at runtime as nodes
join and die). So this project — like Dynamo, Cassandra, and Riak — makes
**every node a coordinator**: whatever node the client happens to hit resolves
the replicas and does the right thing. That's the `Coordinator` struct, which
holds exactly the three things routing needs: the local `Store` (V1), the
`Membership` view with its ring (V2+V3), and the `replication_factor`.

The decision tree for a `GET /cache/hello` arriving at **cache-b** (not a
replica of `hello`):

```
client ──GET /cache/hello──▶ cache-b (coordinator, picked by the client's LB / whim)
                               │
                               │ replica_nodes("hello") = [cache-c, cache-a]   (alive only)
                               │ am I in the set?  no →  forward
                               ▼
                     GET /internal/cache/hello ──▶ cache-c ── local_get ──▶ bytes
                               │◀───────────────────────────────────────────┘
                               ▼
client ◀──── 200 + bytes ── proxy the answer back (client never learns who owned it)
```

Three details of the scaffold map straight onto this:

- **The two-tier HTTP surface** in [routes.rs](../src/routes.rs):
  `/cache/{key}` (public) calls `coordinator.get/put/delete` — the routing
  brain; `/internal/cache/{key}` calls `local_get/local_put/local_delete` —
  store-only, no routing. The separation prevents infinite forwarding loops (a
  forwarded request cannot be re-forwarded — it hits the local-only path) and is
  also a security boundary (see
  [04-fundamentals-woven-through.md](04-fundamentals-woven-through.md)).
- **`replica_nodes`** (already implemented) resolves the ring's replica ids,
  filters out members not currently `Alive`, and resolves ids to addresses —
  which is exactly the V4 criterion "a request never routes to a node marked
  dead", falling out of V3's membership automatically.
- **The forwarding cost** is one extra network hop whenever the client guessed a
  non-replica. The alternative — topology-aware ("smart") clients that hash keys
  themselves and connect straight to a replica — saves the hop but pushes ring
  and membership knowledge into every client library. Dynamo-style systems
  accept the hop for dumb clients; memcached historically chose smart clients.
  This project chooses the coordinator; know what you're paying (CONCEPTS.md's
  Card 4 probe).

## 4. The dials: N, R, W — and why R+W>N means overlap

Replication creates the consistency question. A write must reach several nodes;
those arrivals are not instantaneous or guaranteed. So every replicated system
answers three independent questions:

| Dial | Question | This project's default |
|---|---|---|
| **N** | how many nodes hold a copy? | `REPLICATION_FACTOR: 2` |
| **W** | how many must acknowledge a write before the client gets `204`? | yours to choose |
| **R** | how many do you consult on a read? | yours to choose |

The classic theorem is pure pigeonhole. If **R + W > N**, then any read set and
any write set must share at least `R + W − N ≥ 1` node: with N=3, W=2, R=2, the
write touched 2 of 3 nodes and the read touches 2 of 3 — two subsets of size 2
drawn from 3 elements *cannot* be disjoint (that would need 4 elements). So
every read overlaps the latest completed write and can see its value.

The two ends of the spectrum, concretely, for N=2:

| Policy | `PUT` returns after | A read can be stale when… | Latency & availability |
|---|---|---|---|
| **W=1, async replicate; R=1** | first replica acks; second copy sent in the background | you write to replica-1, it dies before the background copy lands, reads hit replica-2 → miss/old value | fastest; a write succeeds even with one replica down |
| **W=N; R=1** | both replicas ack | (writes can't complete with any replica down) | write latency = slowest replica; one dead node blocks all writes to its keys |
| **R+W>N (e.g. W=2, R=1 here)** | quorum | overlap guarantees the read *can* see the newest completed write | middle ground |

**Why a cache gets to pick the fast column.** Walk the worst case of W=1 async
(CONCEPTS.md's depth probe): the write lands on cache-c, which dies 5 ms later,
before replicating. Reads fail over to cache-a, which never got the value → a
**miss**. The client re-fetches from the source of truth and re-populates.
Total damage: one extra database read. Now imagine the same staleness in a
*ledger*: money silently vanishes. Same mechanism, catastrophically different
cost — which is why a cache can choose **availability + speed** and admit
staleness, while a database must pay for coordination. This is the entire
point of V4's last criterion: *"it's a cache, so name what you gave up."* Your
`docs/07-design.md` must state N, your read/write policy, and the staleness it
admits. (Project 09 builds Raft — the opposite end of this spectrum — hold both
in your head and you own the whole tradeoff.)

Two honesty notes, because precision here is what the CONCEPTS.md trap grades:

- **R+W>N is *not* "strong consistency."** It guarantees read/write set overlap
  — it does not order two *concurrent* writes (last-write-wins? vector clocks?
  someone must decide), and failovers can still surface anomalies. Don't claim
  more than the pigeonhole gives you.
- **Quorums don't give you transactions or CAS** — there's no "compare" anywhere
  in this machinery, just overlapping reads and writes of independent keys.

## 5. Membership changes: ownership must recompute *now*

The coordinator recomputes `replica_nodes(key)` from the **current** ring on
every request — it never caches a routing decision. That's deliberate: V3 can
change the ring at any moment (join, death, refutation), and the V4 criterion
requires that "when membership changes, ownership recomputes from the current
ring."

What it deliberately does *not* do (and the caching horizontal asks you to
document): move data when ownership changes. A key whose replica set shifts
simply cold-misses on its new owner. The stretch patterns that close this gap —
worth knowing by name even if you skip them:

- **Hinted handoff**: a replica that couldn't be reached at write time gets its
  writes parked on another node with a "deliver when it returns" hint.
- **Read-repair**: a coordinator that reads from multiple replicas and sees them
  disagree writes the newest value back to the stale ones — reads heal the data.

Both are how Dynamo-lineage systems make a rejoining node useful again without a
full re-sync.

## 6. Mental model summary

| Question | Answer to hold onto |
|---|---|
| Why replicate a *cache*? | Not for durability — to prevent a node death from becoming a synchronized cold-miss storm into the database |
| Where do copies live? | The key's N distinct ring successors — V2's `replicas()` already computes it |
| What's a coordinator? | Whatever node the request hit: resolve replicas, serve locally if owner, else forward to `/internal/...` and proxy back |
| Why two HTTP paths? | `/cache` routes, `/internal/cache` is local-only — kills forwarding loops and marks the trust boundary |
| N, R, W in one line | copies, write-acks, read-consults — three independent dials, not one "consistency setting" |
| Why does R+W>N overlap? | Pigeonhole: two subsets of sizes R and W from N elements can't be disjoint when R+W>N |
| Why can a cache choose W=1? | Its worst staleness case degrades to a miss + one source-of-truth re-read; a ledger's degrades to wrong money |

## 7. Where you'll build this

Everything lands in [coordinator.rs](../src/coordinator.rs) — `replica_nodes`
and the three `local_*` operations are wired; yours are:

- **`get`**: resolve replicas → serve locally if you're one → else forward
  (`GET /internal/cache/{key}` on the replica's `http_addr`) with **read
  failover** to the next replica on error — and your R choice,
- **`put`**: fan out to every replica (local via `local_put`, remote via
  internal PUT) and decide **when to ack** — your W choice, the graded decision,
- **`delete`**: the same fan-out, removing everywhere.

This doc unlocks the five **Done when ALL true** boxes of **V4** in
[SPEC.md](../SPEC.md), whose Proofs are integration tests you can stage on the
compose cluster: write a key, `docker compose kill` one of its replicas, read it
back through another node; and ask a non-owner and an owner for the same key and
get the same answer. Decisions you own (→ `docs/07-design.md`): replication
factor, W and R, ack timing, failover order, and the staleness statement. The
build itself belongs to `/hint` and `/quest`; the Dynamo paper (DeCandia et al.,
2007) is the canonical deep-dive if you want the original blueprint.
