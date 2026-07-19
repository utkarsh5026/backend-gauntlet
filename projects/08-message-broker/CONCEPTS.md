# Concept Bank — Project 08: Mini Message Broker (Kafka-lite)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The segmented append-only log *(V1 · `src/log.rs`)*

**The problem.** A broker must absorb writes at disk speed, never lose a committed record, and let history be deleted cheaply. A database table fights all three. And the durability part hides two lies: `write()` returning doesn't mean bytes are on the platter (the page cache is between you and the disk), and a crash mid-append leaves a *torn tail* — half a record that, read back naively, is garbage served as data.

**The idea.** One partition = one append-only log: records framed with length + CRC, appended to the tail of the active **segment** file; when it fills, roll to a new one, named by its base offset. Sequential appends are the fastest thing a disk (spinning *or* SSD) does — that's the entire performance story of Kafka in one sentence. Durability is a dial you set consciously (fsync per record vs batched), and recovery scans the tail: a frame whose length/CRC don't check out marks the truncation point — you lose at most the in-flight write, never a completed one, and never serve garbage.

**In the wild:** Kafka's log (this is a faithful miniature), Postgres/MySQL WALs, Raft log storage, every event-sourcing store; segment-per-file retention is exactly how Kafka expires data.

**You own it when you can explain:**
- [ ] Why sequential appends beat random writes by orders of magnitude, on SSDs too (write amplification, erase blocks) — not just spinning rust.
- [ ] The full journey of a write: your buffer → page cache → platter, and which arrows `write()`, `flush()`, and `fsync()` each move.
- [ ] The fsync dial: per-append (slow, zero loss) vs every-N/every-T (fast, bounded loss) — and who should choose (the ack contract).
- [ ] Torn-tail recovery: how length+CRC framing turns "crashed mid-write" into "truncate to last valid frame".
- [ ] Why segments make retention O(1) (delete a file) instead of O(data) (rewrite a file), and how base-offset filenames make offset→segment lookup a directory listing.

**Depth probes:**
- Group commit: how does one fsync amortize over 50 queued producers, and what latency does the 50th producer pay?
- Why is CRC needed *in addition to* fsync? (fsync orders writes; it doesn't detect bit rot or partial sector writes.)

**Trap:** trusting a green test suite as proof of durability. Tests rarely crash between `write` and `fsync` — the torn-tail and kill-mid-append tests exist because the happy path can't distinguish a durable log from a lucky one.

---

## 🧠 Card 2 — The sparse index: seek, don't scan *(V2 · `src/index.rs`)*

**The problem.** A consumer says "give me records from offset 4,000,000". Offsets are logical, but disk positions are bytes — without help you'd scan 4M records to find the byte where the request starts. A *dense* index (every offset → byte position) fixes the scan but costs an index entry per record — at billions of records, the index becomes its own storage problem.

**The idea.** Index *sparsely*: one `(relative_offset → byte_position)` entry every ~N bytes. Resolve a fetch by picking the segment (base offset ≤ K), binary-searching the index for the largest entry ≤ K, seeking there, and scanning forward at most one interval. O(log entries) + a bounded scan — and the interval is a knob trading index memory against scan length. Crucially the index is a *rebuildable hint*: the log is the truth, so a deleted or corrupt index is an inconvenience (rebuild by scanning), never data loss.

**In the wild:** Kafka's `.index` files are exactly this; sparse indexing is also the skeleton of LSM SSTable block indexes (project 22) and search-engine term dictionaries (project 20).

**You own it when you can explain:**
- [ ] The three-way tradeoff: no index (O(n) scan) vs dense (O(1), huge memory) vs sparse (O(log)+bounded scan, tiny memory).
- [ ] The full resolution path for "fetch from offset K", segment choice included.
- [ ] Why the index stores *relative* offsets (base offset lives in the filename — entries fit in fewer bytes).
- [ ] The hint-not-truth principle: what property makes an index rebuildable, and why derived data should never be the only copy of anything.
- [ ] Why fetch-at-log-end must return "nothing yet" cleanly — a tailing consumer lives at that boundary permanently.

**Depth probes:**
- What interval would you pick for a 1 GB segment and why? What does halving it cost and buy?
- Where else in this repo does "derived structure, rebuildable from the log" appear? (Raft state machines, workflow replays, SSTable indexes.)

**Trap:** writing the index synchronously and treating index corruption as fatal. Both come from forgetting its status: it's a cache of the log's geometry, not a peer of the log.

---

## 🧠 Card 3 — Partitions: ordering vs parallelism *(V3 · `src/topic.rs`)*

**The problem.** One log has one writer path and one total order — which caps throughput at one sequential stream and consumers at one reader. But most ordering demands are narrower than "everything in order": you need *this user's* events in order, not user A's relative to user B's. Global order is expensive and mostly unneeded; no order is cheap and mostly unusable.

**The idea.** A topic = N independent logs (partitions). Keyed records route by `hash(key) % N` — same key, same partition, forever — so order is total *within* a partition and undefined across. Keyless records spread round-robin. This is the deal the whole streaming world runs on: per-key order (the order that matters) in exchange for N-way parallelism. The fine print: partition count is effectively fixed — change N and `hash(key) % N` sends every key somewhere new, so "repartitioning" is a data migration wearing a config flag's clothes.

**In the wild:** Kafka/Redpanda partitions, Kinesis shards, Pulsar partitioned topics, NATS JetStream — identical shape everywhere; "how many partitions?" is a perennial capacity-planning fight because of the fixed-N fine print.

**You own it when you can explain:**
- [ ] Why per-key ordering is the only ordering guarantee worth making — a concrete bug that cross-key ordering wouldn't prevent anyway, and one that per-key ordering does.
- [ ] The stability requirement on the partitioner, and what breaks (key order) the day it changes.
- [ ] Why partition count caps consumer parallelism (≤ one consumer per partition per group), making N a capacity decision.
- [ ] Why offsets are per-partition — what a global offset would require (coordination across independent logs — the thing you removed on purpose).
- [ ] Hot partitions: what a skewed key (one huge tenant) does, and the mitigations (key salting, splitting the tenant).

**Depth probes:**
- Orders and payments both keyed by `order_id` — same partition? What does putting them in one topic vs two do to ordering and consumers?
- Why does Kafka's "add partitions" operation carry a warning about keyed data? Reconstruct the reason from first principles.

**Trap:** claiming a topic is "ordered". Only a partition is ordered. Any consumer reading two partitions merges them in nondeterministic order — code that assumed topic-order works in dev (1 partition) and breaks in prod (12).

---

## 🧠 Card 4 — Consumer groups & the delivery contract *(V4 · `src/group.rs`)*

**The problem.** Many consumers want to split the work; the same stream also feeds *other* teams independently; consumers crash and must resume where they left off, not at zero and not past unprocessed records. All of that lands on one deceptively small question: *where does the cursor live, and when does it advance?*

**The idea.** A **consumer group** shares one durable cursor per partition. Within a group, each partition is owned by at most one member (parallelism without double-reads); different groups have independent cursors (fan-out to many teams). The delivery guarantee is decided by commit ordering alone: commit *after* processing → a crash re-reads → **at-least-once** (duplicates possible, loss impossible); commit *before* → **at-most-once** (loss possible, duplicates impossible). You choose loss or duplicates; "neither" isn't on the menu — the broker's at-least-once plus an idempotent consumer is how the industry spells "exactly-once".

**In the wild:** Kafka consumer groups + `__consumer_offsets`, SQS in spirit, Kinesis checkpointing; every stream-processing framework's "exactly-once" (Flink, Kafka Streams) is at-least-once + dedup/transactions underneath.

**You own it when you can explain:**
- [ ] The group as *both* the unit of parallelism and the unit of progress — and why those coupling is what makes rebalancing tricky.
- [ ] Commit-ordering ⇒ delivery-guarantee, as a two-case proof sketch from the crash timing.
- [ ] Why two groups on one topic are fully independent (the log is immutable; cursors are per-group bookmarks — reads don't consume).
- [ ] What a rebalance must guarantee mid-flight (no partition unowned, none double-owned) and why a member joining forces reassignment.
- [ ] Why a returning consumer resumes from the *committed* offset, and what the gap between "processed" and "committed" costs on restart.

**Depth probes:**
- A consumer processes a record, writes to its DB, then crashes before committing the offset. The record is redelivered. Design the consumer so this is harmless.
- Why does log compaction (keep latest per key) exist as an alternative to time retention — what use case (changelog/table) does it serve?

**Trap:** committing offsets on a timer "for simplicity". Auto-commit can commit *past* records still being processed — turning your at-least-once into silent at-most-once exactly when a crash happens.

---

## ⚡ Rapid-fire round

- [ ] Consumer lag = log-end offset − committed offset: why it's *the* broker health metric and what a steadily climbing lag forces you to decide.
- [ ] Why produce/fetch are batched on the wire, and fetch is always bounded (max_records/bytes) — a fetch must never be "the whole log".
- [ ] Why appends to one partition serialize through a single writer while reads stay concurrent — the deliberate contention model.
- [ ] Topic names become directories: the path-traversal validation that implies.
- [ ] Graceful shutdown: fsync active segments + committed offsets, so restart finds no torn tail and no lost cursor.
- [ ] Retention deletes whole segments, never rewrites live ones — the V1 payoff, visible operationally.

## 🔗 Connects to

- Project 05 *consumed* a broker exactly like this — now you know what JetStream was doing under you (ack-after-write, redelivery, cursors).
- The log-is-truth / derived-state-is-rebuildable principle is Raft's core in project 09 and event sourcing's in project 21.
- Torn-tail recovery and the fsync dial return, hardened, in project 22's WAL.
