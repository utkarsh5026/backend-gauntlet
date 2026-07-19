<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 08 — Mini Message Broker (Kafka-lite)

> A message broker looks like a queue with extra steps — until you ask it to
> never lose a committed message, never reorder within a key, hand the same
> stream to many independent consumers, and do it while a producer is hammering
> it at hundreds of MB/s. Kafka's answer to all of that is one deceptively
> simple idea: **an append-only log on disk, split into segments, split into
> partitions, read by cursor.** This project builds that log from scratch. It's
> Tier 4 because the hard parts — durable sequential writes, seeking into a
> multi-gigabyte file by logical offset, and at-least-once delivery across a
> consumer group — are exactly the parts a `cargo add rdkafka` would hide.

## What it does (the easy part)
- `POST /topics` `{name, partitions}` → create a topic with N partition logs.
- `POST /topics/{topic}/records` → **produce** a batch of records; each gets a
  `(partition, offset)`.
- `GET /topics/{topic}/partitions/{p}/records?offset=&max_records=` → **fetch** a
  batch starting at an offset, plus the `next_offset` to continue from.
- `POST /groups/{group}/members` / `POST /groups/{group}/offsets` → join a
  **consumer group** and commit progress; a returning consumer resumes where the
  group left off.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the
> system must do*, never *how*; figuring out the how is the entire point. A box
> only flips to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Segmented append-only log — *the durable commit log*
The foundation. One partition is one **log**: an ordered, append-only sequence of
records living in a directory of fixed-size **segment** files (`…0000.log`,
`…4096.log`, named by the base offset they start at). Producing appends a
length-and-CRC-framed record to the tail of the active segment and returns a
monotonic offset; when the active segment fills, it **rolls** to a new one.
Reads never mutate. Build it in `src/log.rs`.

The trap is durability under a crash. A write is not "done" when `write()`
returns — it's done when the bytes and the directory entry are on the platter.
And a half-written frame at the tail after a crash must be *detected and
truncated on recovery*, never handed to a consumer as if it were real.

**Done when ALL true:**
- [ ] Appending N records returns **monotonically increasing** offsets (0, 1, 2, …); the offset of a record equals the number of records appended before it.
- [ ] Records **survive a process restart**: reopening the log reads back exactly what was appended, in order, at the same offsets.
- [ ] A record read back is **byte-identical** to what was written, and a **corrupted frame is detected** (CRC/length mismatch) rather than silently returned.
- [ ] The log **rolls to a new segment** once the active one exceeds the size cap — observable as more than one segment file on disk for a large enough log.
- [ ] Segment filenames **encode their base offset**, so the segment holding a given offset is found without opening every file.
- [ ] A **crash mid-append** (a torn tail frame) is truncated on recovery — the log reopens at a clean record boundary, losing at most the in-flight write, never a completed one.
- [ ] The **fsync / durability policy** is a *deliberate, documented* choice (per-append vs. batched every N records / T ms), not an accident of whatever the OS flushed.

**Proof:** round-trip + restart-recovery tests; a torn-tail test (write a partial
frame, reopen, assert clean truncation); a corruption test (flip a byte → read
errors, never returns bad data); a `bench/` append-throughput number (records/s
and MB/s) at the chosen fsync policy. `docs/08-design.md` names the on-disk frame
format and the durability policy.

*Concept to internalize:* why append-only + sequential writes are the fastest
thing a disk does, why segmentation makes retention a cheap file delete, and the
throughput-vs-durability dial that is `fsync`.
**Stretch:** a background retention worker that deletes whole segments past a size
or age bound (never rewrites a live one).

### V2. Sparse offset index — *seek, don't scan*
A consumer that wants offset 4,000,000 must not read 4,000,000 records to get
there. Alongside each segment, maintain a **sparse index**: `(relative_offset →
byte_position)` entries written roughly every `index_interval_bytes`. To resolve
a fetch at offset K: pick the segment (base offset ≤ K), binary-search its index
for the largest indexed offset ≤ K, `seek` to that byte position, then scan
forward the last few records to K. Build it in `src/index.rs`.

**Done when ALL true:**
- [ ] A fetch from an **arbitrary offset** returns the record at that offset and everything after it — correct regardless of how deep into the segment it lands.
- [ ] Locating an offset is **sub-linear**: the bytes scanned to reach it are bounded by `index_interval_bytes`, *independent* of the offset's distance from the segment start.
- [ ] The index is **sparse** — far fewer entries than records (about one per interval) — and the interval is a documented, tunable knob.
- [ ] The index is a **rebuildable hint, not the source of truth**: delete it, reopen the log, and reads still resolve correctly (it is reconstructed from the log).
- [ ] A fetch at an offset **at or past the log end** returns "no records yet" cleanly — the tailing-consumer case — not an error and not a hang.

**Proof:** a seek test asserting a mid-log fetch scans ≤ one interval of bytes
(instrument the read) rather than full-scanning; an index-rebuild test (rm the
index → reads still work); a `bench/` comparing fetch-from-offset latency **with
vs. without** the index as the log grows.

*Concept to internalize:* the memory-vs-seek tradeoff of a sparse index, why
O(log n) + a bounded scan beats both a dense index and a linear scan, and why the
index must be reconstructible.

### V3. Partitions & the topic — *ordering vs. parallelism*
A topic is **N independent logs** (partitions). Producing to a topic picks one
partition: by **key** (same key → same partition, forever) when the record has a
key, or spread (round-robin) when it doesn't. Order is total **within** a
partition and **undefined across** partitions — that's the deal that buys
horizontal throughput. Offsets are **per-partition**. Build it in `src/topic.rs`.

**Done when ALL true:**
- [ ] A topic created with N partitions has **N independent logs**; each produced record lands in **exactly one** of them.
- [ ] Records with the **same key always route to the same partition** — a stable mapping that holds for the life of the topic.
- [ ] **Keyless records spread** across partitions — no single partition is hot by default.
- [ ] **Per-partition order is total**: within one partition, consume order == produce order. No ordering is claimed across partitions (and the design doc says so out loud).
- [ ] **Offsets are per-partition**, not global — each partition owns its own 0…n offset space.

**Proof:** tests for key→partition stability (same key, many produces, one
partition), keyless spread (roughly even across partitions), and per-partition
FIFO; a `docs/08-design.md` note on the partitioner and why partition count is
fixed at create time (changing N remaps every key).

*Concept to internalize:* why per-key ordering is the only ordering guarantee
worth making, how partition count caps consumer parallelism, and why re-partitioning
is a migration, not a config change.

### V4. Consumer groups & durable offset commits — *at-least-once delivery*
Many consumers, one shared cursor per group. Members of a **group** split the
topic's partitions between them (each partition owned by at most one member at a
time), and the group **durably commits** how far it has read per partition — the
bookmark that survives a restart. A consumer fetches from its last committed
offset, processes, then commits. If it dies before committing, another member
re-reads from the last commit: **at-least-once**, never silent loss. Build it in
`src/group.rs`.

**Done when ALL true:**
- [ ] A group's **committed offset per (topic, partition) is durable** — it survives a broker restart, so a returning consumer resumes there, not from 0.
- [ ] Within a group, each partition is assigned to **at most one member at a time** — two members of the same group never consume one partition simultaneously.
- [ ] **Two different groups** reading the same topic each get the **full stream independently** — one group's commits don't move the other's cursor.
- [ ] Delivery is **at-least-once**: a consumer that processes but dies *before* committing causes **redelivery** on restart — the commit-after-processing ordering is deliberate and documented.
- [ ] A member **joining or leaving** triggers a reassignment so every partition stays owned while any member is present (no partition left unconsumed).

**Proof:** a restart test (group resumes from its committed offset); a two-member
test (exclusive partition ownership); a two-group test (independent progress); a
crash-before-commit test (redelivery, not loss); `docs/08-design.md` states the
delivery guarantee and the commit ordering that produces it.

*Concept to internalize:* at-least-once vs. at-most-once as a choice of *when you
commit*, why exactly-once needs more than a broker, and consumer groups as the
unit of both parallelism and shared progress.
**Stretch:** rebalance mid-flight (a member leaves under load) without dropping or
double-owning a partition; log **compaction** (keep only the latest value per key).

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Batched produce & fetch:** produce accepts *many* records in one request; fetch returns a *bounded* batch plus the `next_offset` to continue — a fetch can never be asked to return the whole log at once (bounded by `max_records` / a byte cap).
- [ ] **Graceful shutdown:** on SIGTERM, in-flight appends finish and the active segments + committed offsets are fsync'd before exit — a clean restart loses nothing and finds no torn tail.
- [ ] **Wire format documented:** the produce/fetch request & response shapes (and how record bytes are encoded over JSON — UTF-8 vs. base64) are written down. *(Stretch: a length-prefixed binary TCP protocol instead of HTTP/JSON.)*

### Storage lifecycle
- [ ] **Retention** deletes *whole* segments past a size or age bound and never rewrites a live segment — the payoff of V1's segmentation, verifiable by segment count dropping under load.
- [ ] Reads stream from the segment file and **don't buffer a whole segment in RAM** to answer a fetch.

### Security
- [ ] **Auth on produce/admin:** an open produce endpoint is an open disk — writes (and topic creation) sit behind a credential; keys are never logged.
- [ ] **Name & size validation:** topic names and keys become **path components** on disk — reject path-traversal / illegal names, and enforce a **max record size** so one client can't stream you out of disk. Each with a test.

### Observability
- [ ] `tracing` span per request (via `common-telemetry`), with a request id.
- [ ] Structured logs on the events that matter: **segment roll**, **retention delete**, **group rebalance**.
- [ ] Metrics at `/metrics`: **produce rate & bytes-in**, **per-partition log-end offset**, and **consumer group lag** (log-end offset − committed offset) — lag is *the* broker health metric.

### Cross-cutting scale skills
- [ ] Appends to one partition **serialize** (single writer) while reads stay concurrent — the contention model is deliberate, not incidental.
- [ ] The `bench/` reports **end-to-end produce→consume throughput** as partition count rises, showing partitions actually buy parallelism.

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains real numbers in `docs/08-benchmarks.md`: **append throughput**
   (records/s, MB/s) at the chosen fsync policy; **fetch-from-offset latency with
   vs. without** the sparse index as the log grows; **end-to-end throughput vs.
   partition count**.
3. `docs/08-design.md` records the four decisions the SPEC grades: the **on-disk
   record/segment frame + fsync policy** (V1), the **index sparsity interval**
   (V2), the **partitioning function** (V3), and the **delivery guarantee + commit
   ordering** (V4).
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p message-broker`
   are green; no `todo!()` remains on a checked path.

## Suggested order of attack
1. Boring path: one partition = one growing file; append, and fetch by scanning
   from 0. Offsets in memory. Prove produce→fetch round-trips.
2. **V1:** real segmented log — framing + CRC, segment rolling, restart recovery,
   torn-tail truncation, a chosen fsync policy.
3. **V2:** the sparse index + seek; then prove a mid-log fetch doesn't full-scan.
4. **V3:** N partitions behind a topic + the partitioner (keyed + keyless).
5. **V4:** consumer groups — durable per-group offset commits, then assignment
   across members, then prove at-least-once with a crash-before-commit test.
6. Retention, auth + validation, and the metrics (produce rate, log-end offset,
   lag).
7. Benchmark, document, tune.

## Run it
```bash
cp .env.example .env        # then adjust DATA_DIR / segment size if you like
cargo run -p message-broker # no external deps — the filesystem IS the log
```
