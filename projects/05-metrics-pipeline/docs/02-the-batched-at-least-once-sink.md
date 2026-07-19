# The Batched, At-Least-Once Sink — From First Principles

> How rollups get out of memory and into a column store without melting it,
> why "exactly-once" is a menu you can't order from, and how a bounded buffer
> turns a slow database into backpressure instead of an OOM. No prior
> knowledge of column stores or message brokers assumed.
>
> This prepares you for **V3** in [SPEC.md](../SPEC.md) — the sink you'll
> build in [sink.rs](../src/sink.rs), driven by the consumer loop in
> [pipeline.rs](../src/pipeline.rs), landing in the ClickHouse table defined
> in [0001_init.sql](../migrations/0001_init.sql). Card 3 in
> [CONCEPTS.md](../CONCEPTS.md) is the checklist this doc unlocks.

---

## 0. The one sentence to hold onto

**Flush in micro-batches on a size-*or*-time trigger, ack the broker only
*after* the durable write, and make the write idempotent — because
ack-after-write guarantees at-least-once, and at-least-once guarantees
duplicates.**

---

## 1. The problem: column stores punish small writes

ClickHouse (like every column store) organizes data as large, immutable,
column-oriented **parts** on disk. Every `INSERT` — no matter how small —
creates a new part: files per column, metadata, an entry for the background
merge scheduler that will later combine small parts into big ones. The store
is built for *few, huge* appends. Feed it the opposite and:

| One `INSERT` per rollup row | What happens |
| --- | --- |
| 10,000 rows/sec → 10,000 parts/sec | Part creation overhead dwarfs the data itself |
| Merge scheduler drowns | Background merges can't keep up with part creation |
| ClickHouse defends itself | The infamous `Too many parts` error — it starts rejecting your writes |

Batch the same 10,000 rows into **one** insert and it's one part, one
round-trip, one merge-queue entry. This is not a 20% tuning win; it's the
difference between working and falling over. The [sink.rs](../src/sink.rs)
doc-comment calls micro-batching "the column-store contract", and the
Definition-of-done bench in [SPEC.md](../SPEC.md) has you *measure* the gap
(batched vs row-at-a-time is one of the required numbers).

---

## 2. Micro-batching: the size-*or*-time trigger

So the sink buffers rows and flushes in batches. Flush *when*? A size-only
trigger has a nasty failure mode, so the answer is a **dual trigger** —
whichever fires first — with both knobs already in
[.env.example](../.env.example):

```
BATCH_MAX_ROWS=10000        # size trigger  — checked in Sink::push()
BATCH_MAX_DELAY_MS=1000     # time trigger  — the pipeline's flush ticker
```

| Traffic | What fires | What you get |
| --- | --- | --- |
| Busy (≥10k rows/sec) | Size trigger, constantly | Big, efficient inserts — the throughput case |
| Quiet (3 rows/min) | Time trigger, every 1 s | Bounded staleness — a rollup is never held hostage more than ~1 s |
| Size trigger only, quiet hours | *nothing* | The last few rollups sit in RAM **indefinitely** — invisible on the dashboard, lost on a kill. This is the trap Card 3 names |

The split is visible in the scaffold: [`Sink::push()`](../src/sink.rs) owns
the size trigger (`todo!()`), while the `flush_ticker` arm of the
`tokio::select!` loop in [pipeline.rs](../src/pipeline.rs) drives the time
trigger by calling [`Sink::flush()`](../src/sink.rs) on an interval. Batch
size trades throughput against latency and RAM; that knob — "the whole game",
per the SPEC — is yours to tune with the bench.

---

## 3. Delivery semantics: where you put the ack decides what you lose

The sink doesn't read from thin air — it consumes from NATS JetStream, a
durable log. JetStream (like Kafka) redelivers any message that isn't
**acknowledged** within its ack-wait (the consumer is created with
`AckPolicy::Explicit` in [`setup_consumer()`](../src/pipeline.rs) for exactly
this reason). That gives you precisely one decision, with two possible
answers:

```
consume msg ──▶ roll up ──▶ write batch to ClickHouse ──▶ ack
                                                           ▲
              ┌── ack HERE (before write) ─────────────────┘── or HERE (after)
```

| | Ack **before** the write | Ack **after** the write |
| --- | --- | --- |
| Crash between the two | Broker thinks it's delivered; batch in RAM is gone → **data silently lost** | Broker redelivers; batch is written **twice** → duplicates |
| Name | At-most-once | At-least-once |
| For metrics history | Wrong — gaps in dashboards | Right — *if* you can neutralize duplicates |

There is no third option. "Exactly-once" between a broker and an external
store is not a delivery guarantee you can pick; it's at-least-once **plus an
idempotent write** — which is the next section. (The scaffold is honest about
its own placeholder: [`process_message()`](../src/pipeline.rs) currently acks
right after ingest, and its `TODO(V3)` tells you that acking there "would turn
a crash into silent data loss" — moving the ack after the durable flush is
part of your V3 work.)

Note what the broker bought you here: because a `202`-accepted point lives in
JetStream's replayable log (with a durable consumer that remembers its
offset — `DURABLE_NAME` in [.env.example](../.env.example)), the entire
consumer process can crash and restart and *nothing accepted is lost*. A
`tokio::mpsc` channel between ingest and rollup could never promise that —
that's what earns NATS its place in [docker-compose.yml](../docker-compose.yml).

---

## 4. Idempotency: making duplicates collapse

At-least-once *will* hand you the same rollups twice. The fix is to make the
write **idempotent**: replaying it must leave the store as if it ran once.

The key insight is that a rollup row already has a natural identity:
`(series_id, window_start, window_secs)` — deterministic *from the data
itself*, identical on every redelivery. The schema in
[0001_init.sql](../migrations/0001_init.sql) turns that into the dedup lever:

```sql
ENGINE = ReplacingMergeTree(inserted_at)          -- newest insert wins on a dup key
ORDER BY (series_id, window_start, window_secs)   -- the ORDER BY *is* the dedup key
```

`ReplacingMergeTree` collapses rows with an identical sort key during
background merges, keeping the newest (`inserted_at` is the version column).
A replayed batch re-inserts the same keys, and they fold back into one row.
Two fine-print items you must carry into the implementation:

- **Dedup is eventual.** Collapsing happens at merge time, not insert time —
  a read in between sees both copies. That's why the read path
  ([`query_range()`](../src/sink.rs)) must use `FINAL` or aggregate over the
  key, as its `TODO` and the migration's comments both say.
- **The key must be deterministic.** Sneak anything non-deterministic into
  identity (an insert timestamp, a random id) and every redelivery mints a
  "different" row — dedup silently stops working. This is why
  `inserted_at` is the *version*, explicitly "not part of identity" per the
  schema comment.

Trace one crash end-to-end: batch of 500 rows written → crash before ack →
JetStream redelivers → the same 500 `(series_id, window_start, window_secs)`
keys are re-inserted → `ReplacingMergeTree` folds them on merge → `FINAL`
reads see each window once. At-least-once became *effectively-once for this
data shape* — not by magic, but because the data has a natural key and the
table knows it.

---

## 5. Backpressure: the bounded buffer as a circuit

Last failure mode: ClickHouse gets slow (a merge storm, a restart). Rollups
keep arriving. If the sink buffers them in an unbounded queue, your process
absorbs the entire firehose into RAM — CONCEPTS.md: *"a time-bomb with a fuse
length equal to your RAM."* The queue *hides* the outage until it converts it
into a worse one (OOM, and now the buffered data is gone too).

The correct chain, link by link — each link already exists in the scaffold:

```
ClickHouse slow
   └─▶ Sink::flush() blocks / errors           (sink.rs)
        └─▶ buffer stays full → push() can't accept more
             └─▶ consumer loop stops pulling messages   (pipeline.rs select loop)
                  └─▶ unacked/unfetched msgs accumulate IN THE BROKER
                       └─▶ JetStream holds the backlog ON DISK, replayable
                            └─▶ nothing OOMs; consumer catches up when CH recovers
```

The whole trick: **the backlog must land in the component built to hold one**
(a durable log on disk), not in the component that dies from it (your heap).
A bounded buffer isn't a limitation — it's the signalling mechanism that
pushes the problem upstream to safety. The horizontal checklist's "bounded
buffers everywhere on the write path, caps tuned together" is this principle
applied to every hop.

Set this next to its mirror image now, because V4 inverts it: the durable
path **must not drop, so it slows the producer**. The live SSE path **must
not slow the producer, so it drops**. Doc
[03](03-sse-fan-out-and-load-shedding.md) is that story.

---

## 6. Graceful shutdown vs crash — why one dropped batch is fine and the other is a bug

A subtlety worth internalizing (it's a Card 3 depth probe): a **crash**
mid-batch loses nothing — unacked messages redeliver, dedup absorbs the
replay; the design covers it. But a **clean shutdown** that drops a partial
batch is a real bug: the process *had* the data and *chose* not to flush.
Redelivery still saves the acked-after-write data, but unflushed partial
windows from the rollup engine would be gone. Hence the shutdown arm in
[pipeline.rs](../src/pipeline.rs): drain the rollup engine
(`drain_all()` — the `TODO(V2)` on that line), push to the sink, one final
`flush()`, *then* exit. At-least-once is the safety net; a clean exit
shouldn't need it.

---

## 7. The design space V3 leaves to you

1. **Ack plumbing** — the scaffold acks per-message in the wrong place; you
   must connect "which broker messages fed this flushed batch" so the ack
   happens after the write. How you associate messages with batches is the
   design problem.
2. **Flush-failure policy** — keep the buffer and retry, or drop it and lean
   on redelivery? (The [`flush()` notes](../src/sink.rs) point one way; know
   what each costs.)
3. **Batch knobs** — tune `BATCH_MAX_ROWS` / `BATCH_MAX_DELAY_MS` against the
   bench, and justify them in `docs/05-design.md`.
4. **The read path** — `FINAL` vs `GROUP BY` in
   [`query_range()`](../src/sink.rs), and what each costs at query time.

`/hint` for nudges, `/quest` to build against acceptance tests — including
the SPEC's kill-the-consumer-mid-batch replay test.

---

## 8. Mental-model summary

| Concept | One-liner |
| --- | --- |
| Parts | Every ClickHouse INSERT = a new on-disk part; too many small ones → merge collapse → `Too many parts` |
| Micro-batch | Buffer rows, flush on size **or** time — throughput when busy, bounded staleness when idle |
| Time trigger | Exists for the quiet hours; without it the last rollups are held hostage |
| Ack placement | Before write = at-most-once (loses); after write = at-least-once (duplicates). Pick your poison |
| Idempotent write | Deterministic key `(series_id, window_start, window_secs)` + `ReplacingMergeTree` → replays collapse (eventually; read with `FINAL`) |
| Backpressure | Full bounded buffer → stop pulling → backlog lives in the broker's durable log, not your heap |
| Crash vs clean exit | Crash mid-batch: covered by redelivery. Clean exit dropping a batch: a bug — drain and flush first |

## 9. Where you'll build this

- [`Sink::push()`](../src/sink.rs) — buffer + size trigger (`todo!()`).
- [`Sink::flush()`](../src/sink.rs) — the one-round-trip batched insert
  (`todo!()`).
- [`query_range()`](../src/sink.rs) — the deduped read path (`todo!()`).
- The ack-after-flush rework flagged in
  [`process_message()`](../src/pipeline.rs).
- The schema you already have: [0001_init.sql](../migrations/0001_init.sql)
  (read its comments — they're the idempotency design in SQL form).

You own it (Card 3 of [CONCEPTS.md](../CONCEPTS.md)) when you can explain:
why column stores punish small writes; both ack placements and their loss
modes; how the dedup key turns at-least-once into effectively-once and why it
must be deterministic; the backpressure chain link by link; and why an
unbounded queue in front of a slow sink is a time-bomb.
