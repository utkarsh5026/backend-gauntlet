# Partitions — Trading Global Order for Parallelism

> What this teaches: why a topic is *many* independent logs, how a record
> decides which one it lands in, and the exact ordering guarantee that deal
> buys — and forfeits. No prior knowledge of Kafka or hashing assumed.
>
> Prepares you for **V3** in [SPEC.md](../SPEC.md) ("Partitions & the topic").
> Anchored to [src/topic.rs](../src/topic.rs) — the `partition_for` `todo!()`
> is the single decision of this vertical — and
> [src/partition.rs](../src/partition.rs) (already-wired plumbing). Docs
> [00](00-the-append-only-log.md) and [01](01-the-sparse-index.md) built the
> one log; this doc multiplies it.

---

## 0. The one sentence to hold onto

**A topic is N independent logs, and a record's key decides which one it
joins — so order is total *within* a partition, undefined *across* them, and
that narrower promise is exactly what lets producers, disks, and consumers
scale N-wide.**

---

## 1. The problem: one log is one lane

V1 built a beautiful log — with two ceilings baked into its correctness:

- **One writer.** Appends assign `next_offset` sequentially, so they must
  serialize ([partition.rs](../src/partition.rs) encodes this: the `Log` sits
  behind a `Mutex`). One log = one append lane, no matter how many CPUs or
  producers you have.
- **One reader per group.** A consumer group (V4) can't split a single log's
  work: there's one cursor, one sequence. Ten consumers on one log are nine
  spectators.

The tempting fix — "just make everything faster" — has no move to make: the
serialization *is* the ordering guarantee. If appends didn't serialize,
offsets wouldn't be a total order.

So question the requirement instead: **who actually needs a total order over
everything?** Concretely: an e-commerce clickstream where `user-42` adds to
cart then checks out, while `user-77` browses.

- `user-42`'s *own* events must stay in order — cart-then-checkout processed
  as checkout-then-cart is a real bug.
- Whether `user-42`'s checkout lands before or after `user-77`'s pageview is
  a fact *nobody's code depends on*. They're causally unrelated events; any
  interleaving is as true as any other.

Global order is expensive and mostly unneeded; no order is cheap and mostly
unusable. The order that *matters* is **per-key** order. That narrower promise
is cheap to keep — and it's the deal the whole streaming world runs on.

---

## 2. The idea: N logs + a routing rule

A `Topic` ([src/topic.rs](../src/topic.rs)) is a name plus a fixed
`Vec<Arc<Partition>>` — on disk, `data/topics/clicks/0/`, `…/1/`, `…/2/`, each
directory a full V1/V2 segmented log, each with **its own independent offset
sequence starting at 0**.

Everything routes through one function — the vertical's single decision point,
`Topic::partition_for` ([topic.rs:96](../src/topic.rs#L96)):

- **Keyed record** → hash the key: `hash(key) % partition_count`. Same key,
  same hash, same partition — *every time, for the life of the topic*.
- **Keyless record** → round-robin via the `next_rr` counter, spreading load
  evenly so no partition runs hot by default.

Traced concretely (using CRC-32 as the illustrative hash — *which* hash you
use is your call, with one constraint coming in §4):

| Key | hash (CRC-32) | % 4 | → partition |
| --- | --- | --- | --- |
| `user-42` | 2,097,592,435 | 3 | 3 |
| `user-77` | 641,802,047 | 3 | 3 |
| `user-1024` | 2,028,993,258 | 2 | 2 |
| *(no key)* | — | — | 0, then 1, then 2, … round-robin |

Note `user-42` and `user-77` collide on partition 3 — fine and expected.
The promise is never "one key per partition"; it's "one *partition* per key".

```
producer ──▶ partition_for(key) ─┬─▶ partition 0   [a0, a1, a2, ...]   ← its own offsets
                                 ├─▶ partition 1   [b0, b1, ...]
                                 ├─▶ partition 2   [c0, c1, c2, c3...]
                                 └─▶ partition 3   [user-42 & user-77's
                                                    events, interleaved,
                                                    each key in order]
```

What the deal buys, at every layer:

- **Producers:** appends to *different* partitions don't contend — N
  partitions, N concurrent append lanes ([partition.rs](../src/partition.rs):
  one `Mutex` *per partition*, not per topic).
- **Disks:** N sequential write streams (or N disks/brokers, in a real
  cluster).
- **Consumers:** a group (V4) can assign each partition to a different member
  — N-way parallel consumption with no double-reads.

And what it costs — stated out loud, because the SPEC demands the design doc
say it: **no ordering across partitions, ever.** A produce returns
`(partition, offset)` (see `Topic::produce`,
[topic.rs:101](../src/topic.rs#L101-L107)); there is no topic-wide offset,
because a global sequence number would require the N logs to coordinate on
every append — reintroducing the single lane you just removed on purpose.

---

## 3. The trap: "the topic is ordered" (it never is)

The most common production bug this design produces, straight from
[CONCEPTS.md](../CONCEPTS.md):

Dev environment: topic created with **1 partition**. Consume order equals
produce order — always, trivially. Code quietly grows an assumption that the
topic is ordered. Production: **12 partitions**. A consumer reading several
partitions merges them in whatever order fetches happen to return —
nondeterministic by design. Events that were "always in order" in dev arrive
interleaved, and the assumption detonates weeks after the code shipped.

Only a *partition* is ordered. Per-key order survives (each key lives in one
partition); *cross-key* order was never promised — it just coincidentally held
at N=1. Your per-partition FIFO test plus the keyless-spread test (the V3
Proof line) are what pin the real contract.

---

## 4. Two constraints that make the partitioner interesting

**Stability across restarts.** "Same key → same partition, forever" quietly
requires the *hash function itself* to give the same answer run after run.
That's less automatic than it sounds — hashers designed for in-process
HashMaps are often deliberately *randomly seeded per process* (Rust's default
`DefaultHasher`/`RandomState` is, as a HashDoS defense). Hash `user-42` with
one of those, restart the broker, and the same key routes somewhere new —
splitting one user's history across partitions and silently breaking per-key
order. The scaffold's TODO ([topic.rs:91-94](../src/topic.rs#L91-L94)) warns
exactly this: *pick a hash that doesn't change run to run*. Which stable hash
to pick is your V3 decision.

**N is fixed — forever.** `hash(key) % N` bakes the partition count into every
key's address. Change N and the map shatters: measured concretely, taking
10,000 keys from 4 partitions to 5 moves **8,046 of them** (~80%) to a
different partition. Every moved key's history is now split across two
partitions — old events in one, new events in another — and per-key order is
gone for all of them. That's why:

- `Topic::create` fixes `partition_count` up front, and `Topic::open`
  ([topic.rs:59](../src/topic.rs#L59)) recovers it by *counting the partition
  directories* — the on-disk layout is the source of truth for N;
- Kafka's "add partitions" operation carries a keyed-data warning;
- "repartitioning" in real systems is a *migration* (new topic, re-produce,
  cut over consumers) wearing a config flag's clothes.

So N is a **capacity decision made at create time**: it caps consumer
parallelism forever (V4: at most one member per partition per group — a
12-partition topic can never use a 13th consumer). Too few partitions and
you can't scale consumption later; too many and you pay per-partition
overhead (files, handles, index memory) for lanes you never use. This is the
perennial "how many partitions?" capacity-planning fight, and it exists
because of this section.

**And the constraint no partitioner can fix — hot keys.** Hashing spreads
*keys* evenly, not *load*: if `tenant-mega-corp` produces 60% of all records,
its partition carries 60% of the load, because per-key order *requires* all
its records in one lane. Mitigations (salting the key to split it across
sub-keys, isolating the tenant) all trade away exactly the per-key ordering
the key was buying — there's no free move, which is why CONCEPTS.md keeps it
as a depth probe rather than a checkbox.

---

## 5. The design space — decisions the SPEC leaves to you

- **Which hash?** Stable across restarts (§4), fast, well-spread. Several
  reasonable answers exist in the workspace; choosing and *defending* one in
  `docs/08-design.md` is the graded decision.
- **Keyless routing details.** The scaffold gives you an `AtomicU64` cursor
  (`next_rr`, [topic.rs:29](../src/topic.rs#L29)) — the memory-ordering and
  wraparound handling on it are small but real Rust decisions.
- **Default N.** [main.rs](../src/main.rs) defaults `DEFAULT_PARTITIONS` to 3;
  your design doc should say what you'd pick for a real workload and why
  (peak consumer parallelism you want to allow — remember it's forever).
- **Where V3's tests bite:** key→partition stability across *many* produces
  (and, if you're honest, across a process restart), roughly-even keyless
  spread, and per-partition FIFO — the three Proof-line tests.

`/hint` for graduated nudges; `/quest V3` for the guided build.

---

## 6. Mental model summary

| Idea | One-line takeaway |
| --- | --- |
| Topic = N logs | Independent V1 logs in sibling directories; nothing is shared but the name and the router. |
| Per-key order | The only ordering promise worth making: total within a partition, undefined across — and say so out loud. |
| Keyed routing | `hash(key) % N` with a *run-to-run stable* hash; same key, same partition, for the life of the topic. |
| Keyless routing | Round-robin — spread by default, no hot partition without a hot key. |
| Offsets are per-partition | A global offset would need cross-log coordination — the single lane you removed on purpose. |
| N is forever | Changing N remaps ~all keys (~80% measured for 4→5); repartitioning is a migration, not a setting. |
| N caps parallelism | ≤ one consumer per partition per group — partition count is a capacity decision. |

**Where you'll build this:** [src/topic.rs](../src/topic.rs) —
`partition_for` ([line 96](../src/topic.rs#L96)) is the only `todo!()`;
`produce` above it is already wired to use your answer. It unlocks all five
**V3 Done-when** boxes: N independent logs, stable key routing, keyless
spread, per-partition total order, and per-partition offsets — plus the
design-doc note on the partitioner and fixed N.

**In the wild:** Kafka/Redpanda partitions, Kinesis shards, Pulsar partitioned
topics, NATS JetStream streams — the identical shape everywhere; project 05
consumed one of these without seeing inside. Now you're building the inside.
