# How the State Machine & Linearizable Reads Work — From First Principles

> A ground-up guide to the payoff half of consensus: how an agreed-upon *log*
> becomes the *map* clients actually query, why that only works if `apply` is
> deterministic and gap-proof, and why a `GET` is the most deceptively dangerous
> operation in the whole system — a node can serve you confidently stale data
> while being *provably wrong about its own authority*. No prior knowledge
> assumed beyond docs [00](00-how-leader-election-works.md) and
> [01](01-how-log-replication-and-commit-work.md).
>
> This prepares you for **V3** of the [SPEC](../SPEC.md): the `apply` /
> `snapshot` / `restore` `todo!()`s in [store.rs](../src/store.rs), and the
> read path enforced in [replication.rs](../src/replication.rs)'s `read`.
> Wired: `Store::get` (the lookup itself), [`Command`](../src/rpc.rs)
> (`Set`/`Delete`/`Noop`), `Store::last_applied`. Ideas here, decisions yours —
> `/hint` and `/quest` for the build.

---

## 0. The one sentence to hold onto

**The log is the truth and the map is merely the log, *reduced* — so identical
state everywhere is a theorem about deterministic folding, not a thing you
synchronize; and a read is only honest when served by a node that has proven,
*at read time*, that it still speaks for a majority.**

Two halves: apply (the fold) and read (the proof). Both look trivial and both
hide an invariant that, broken, produces silent wrongness rather than errors.

---

## 1. The inversion: state is derived, never authoritative

Every system you've built so far treats the *database* as the truth and logs as
a byproduct. Replicated state machines invert that: **the log is the system of
record; the `HashMap` is a cache of "the log, reduced."** If you deleted the
map on every node and re-folded the log, nothing would be lost. (Project 21's
event sourcing is this same inversion at the application layer; project 08's
log made the log durable — Raft made N copies of it *agree*. This module spends
what those bought.)

Why this shape? Because V1+V2 produce exactly one artifact: **a totally-ordered
sequence of committed commands, identical on every node**. If every node feeds
that same sequence through the same *deterministic* fold, every node's map is
identical — with **zero coordination about the map itself**. Nodes never
compare maps, never sync values, never merge. Agreement on the *inputs* plus
determinism of the *fold* equals agreement on the *outputs*:

```
same log      ─┐
               ├──►  same map, on every node, forever
same fold     ─┘
```

Watch it run. The committed log:

```
idx1: Set  x=1      idx2: Set  y=2      idx3: Set  x=7
idx4: Noop          idx5: Delete y
```

| after applying… | node A's map | node B's map |
| --- | --- | --- |
| idx1 | {x:1} | {x:1} |
| idx2 | {x:1, y:2} | {x:1, y:2} |
| idx3 | {x:7, y:2} | {x:7, y:2} |
| idx4 (`Noop` — V2's election no-op reaches the fold and does nothing) | {x:7, y:2} | {x:7, y:2} |
| idx5 | {x:7} | {x:7} |

Byte-identical, provably, without A and B ever talking about `x`.

---

## 2. What "deterministic fold" outlaws — and the two-invariant gate

The theorem above is fragile. Each hazard below breaks one premise, and each
produces **silent divergence** — no error, no crash, just two nodes that answer
the same `GET` differently forever after:

| hazard | broken premise | concrete divergence |
| --- | --- | --- |
| apply idx3 before idx2 | same *order* | fine for {x…} here — but reorder idx3 and idx5's cousin (`Set x` vs `Delete x`) and nodes disagree on whether x exists |
| apply idx3 twice | exactly-*once* | harmless for `Set` (idempotent), fatal the day a command isn't (an `Increment` would double) |
| skip idx2 | no *gaps* | node B is missing y — and every later read of y is wrong |
| fold consults anything but the entry (clock, RNG, iteration order) | same *fold* | two nodes fold the same entry to different results |

The scaffold's [`Store`](../src/store.rs) enforces this with one number:
`last_applied`, "the index of the last entry folded into `data`." The
invariant, straight from the `apply` TODO: **`apply` may only accept the entry
at exactly `last_applied + 1`** — anything else is rejected, not reordered,
not tolerated. One comparison rules out gaps, duplicates, *and* reordering
simultaneously (any wrong order must present some index ≠ `last_applied + 1`
at some point). That's V3's "advances by exactly one per entry and refuses a
gap" box, and it's why [`apply_committed`](../src/replication.rs) (the V2 seam
that drives the fold whenever `commit_index` moves) can stay a dumb loop: the
`Store` itself is the gate.

Note also what `apply` is *fed*: only **committed** entries. An uncommitted
entry might still be truncated away by log repair (doc 01 §3) — folding it into
the map would mean un-folding it later, which a fold can't do. `commit_index`
is the fence; `last_applied ≤ commit_index` always.

---

## 3. Reads: the half everyone gets wrong

Writes got three docs of machinery. Reads look free — the value is *right
there* in `self.inner.lock().unwrap().data`
([`Store::get`](../src/store.rs) is already wired!). The whole of V3's
difficulty is understanding why that lookup, alone, **lies**.

First, the promise we're making. A read is **linearizable** when it never
returns a value older than the newest write that *completed before the read
began*:

```
time ──────────────────────────────────────────────►
client W:  ├── PUT x=2 ──✓ (acked)
client R:                    ├── GET x ──► must be 2 (or newer). 1 is a LIE.
```

If the ack and the subsequent read can disagree, "acknowledged" means nothing —
a user updates their password, refreshes, and sees the old one. Linearizability
is exactly "the system behaves like one single map," which is the entire brand
promise of this project.

Now the two ways a local lookup breaks it:

**The lagging follower** (the obvious one). A follower legally trails the
leader — its `last_applied` may be at idx 40 while the cluster committed 45. A
`GET` served from its map returns the world as of idx 40. This is why V2's rule
says followers *redirect* reads too, not just writes.

**The deposed leader** (the one that gets people). Being *the leader* isn't
enough, because leadership itself can be stale:

```
        ┌────────────┐          ╎           ┌──────────────────┐
        │  L1 (term 4)│    partition        │  F2   F3   F4  F5 │
        │  thinks it  │         ╎           │ elect L2 (term 5) │
        │ still leads │         ╎           │ client: PUT x=2 ✓ │
        └────────────┘          ╎           └──────────────────┘
 client: GET x  → L1 checks role==Leader ✓ → serves its map → x=1   ✗ STALE
```

L1 has heard nothing — no higher term, no rejection — because it's heard
*nothing at all*. Every local check it can make passes. Meanwhile the other
side elected L2 (term 5, entirely legal — they have a quorum) and committed
`x=2`. L1's confident `x=1` violates the timeline diagram above. The cruelty of
this bug: **it passes every test that doesn't manufacture exactly this
partition**, which is why the SPEC's Proof for V3 demands a test that creates a
deposed leader and reads from it — and why the [CONCEPTS](../CONCEPTS.md) card
calls the local-read shortcut *the* trap.

The principle underneath: in the partition diagram, *L1's role field is just a
cached value too*. The only ground truth in Raft is what a **majority**
currently says. So:

> A linearizable read must be served by a leader that has **re-confirmed its
> leadership against a quorum at read time**, and has **applied at least up to
> the commit point that confirmation observed**.

Run that rule against the diagram: L1 tries to confirm with a quorum, can only
reach itself, fails, and refuses or redirects — the SPEC's "does not silently
return stale data" box. Both halves matter: confirming leadership without
waiting for `last_applied` to catch up to the observed commit point still
serves an old map.

---

## 4. The design space: read-index vs lease (your call to make)

Two standard techniques implement that rule. The SPEC requires you to pick one
and *name it, with its assumption*, in `docs/09-design.md` — so here is the
tradeoff, not the choice:

| | **read-index** | **leader lease** |
| --- | --- | --- |
| leadership proof | exchange one heartbeat round with a quorum, *per read* (batchable) | none at read time — rely on "no one else could have been elected yet" |
| where the confidence comes from | a majority answered *me*, *now* | elections need an election timeout to pass; within that window (minus drift margin) I'm safe |
| latency cost | +1 round-trip to a quorum before serving | ~zero — read serves locally |
| the assumption you must be able to say out loud | none beyond Raft's own | **bounded clock drift** between nodes; a paused/skewed clock (VM freeze, GC stall) silently voids the lease and re-opens the stale-read hole |
| in the wild | etcd's default linearizable read | common in latency-sensitive systems that accept the clock assumption |

(etcd also offers an explicitly-weaker `serializable` read — a named, opt-in
stale read served locally. Weak-by-contract is honest; weak-by-accident is the
bug.)

The interesting engineering questions — how a read waits for its read point,
how concurrent reads share one confirmation round, what happens to in-flight
reads on step-down — are exactly the `todo!()` in
[`RaftNode::read`](../src/replication.rs). That's where this doc stops:
`/hint` for nudges, `/quest` to build it.

One small contract note while you're in there: a `GET` for a key that was never
set is a **clean `404`** ([`AppError::KeyNotFound`](../src/error.rs)) — "absent"
is a normal, linearizable answer, not an error path.

---

## 5. The loose thread from V2: at-least-once, and the dedup stretch

Doc 01 ended on a cliffhanger: a client whose leader dies mid-`propose` retries
against the new leader — but the old leader may have committed the command just
before dying. Result: the same logical command can appear in the log **twice**,
legally. Raft's delivery guarantee to the state machine is **at-least-once**.

How much that hurts depends on the command:

| command | applied twice | verdict |
| --- | --- | --- |
| `Set x=2` | x is 2, again | idempotent — harmless *today* |
| `Delete x` | second is a no-op | harmless |
| a hypothetical `Incr x` | x off by one, **on every node, consistently** | corrupt — and note replication didn't diverge; the *history itself* is wrong |

Your V3 command set is accidentally safe, which is exactly why the SPEC makes
dedup a **stretch**, not a requirement — but the standard fix is worth holding:
each client tags commands with a monotonically-increasing **sequence number**,
and the state machine remembers the highest applied per client, turning
apply-at-least-once into apply-at-most-once *inside the fold* (where it's
deterministic and replicated, rather than at the edge, where it isn't). This is
how etcd and TiKV handle retries, and it's the `apply` TODO's stretch line.

---

## 6. Mental model summary

| Mechanism | Question it answers | Failure it prevents |
| --- | --- | --- |
| log-is-truth, map-is-reduction | "what do nodes agree on?" | coordinating/merging state directly |
| deterministic fold | "why are maps identical?" | divergence with no error signal |
| `last_applied + 1` gate | "which entry may apply?" | gaps, duplicates, reordering — one check |
| apply only ≤ `commit_index` | "when may it apply?" | folding entries that later get truncated |
| linearizability | "what does a read promise?" | acked writes invisible to subsequent reads |
| quorum-confirmed reads | "who may serve a read?" | the deposed leader's confident stale answer |
| read-index vs lease | "what does the proof cost?" | (a tradeoff, not a failure — pick and document) |
| client sequence numbers (stretch) | "what about retries?" | at-least-once corrupting non-idempotent commands |

## 7. Where you'll build this

The fold: [`Store::apply`](../src/store.rs) (`Set`/`Delete`/`Noop` + the
sequencing gate). The read: [`RaftNode::read`](../src/replication.rs) (reject
non-leader → confirm leadership → wait for the read point → `store.get`).
Driven by [`apply_committed`](../src/replication.rs) from V2.
(`Store::snapshot`/`restore` also live here but belong to V4 — next doc.)

This doc unlocks V3's **Done when ALL true** ([SPEC](../SPEC.md)):

- [ ] same committed log on two fresh nodes → byte-identical state
- [ ] `last_applied` advances by exactly one per entry; gaps refused
- [ ] a linearizable `GET` never returns older than the last completed committed write
- [ ] a deposed leader does not silently serve stale data
- [ ] unset key → clean `404`

Proofs: the two-Stores determinism test, the gap-rejection unit test, the
partitioned-old-leader linearizability test — and `docs/09-design.md` names
your read technique and its assumption.

Next: [03-how-snapshots-and-log-compaction-work.md](03-how-snapshots-and-log-compaction-work.md) —
because the log you just crowned as the source of truth grows forever.
