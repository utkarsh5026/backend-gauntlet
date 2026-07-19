<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 05 — Time-series Metrics Pipeline

> "Take in a firehose of numbers, store them, and draw a graph." It sounds like
> an `INSERT` and a `SELECT ... GROUP BY`. The trap is the *shape* of the load.
> A metrics pipeline ingests **millions of points per second** — tiny, append-
> only, never updated — and is then asked to answer "p99 latency for service=api,
> region=us, last 6 hours" in under a second. That is two systems fighting each
> other: a write path that must never block on the read path, and a read path
> that can't possibly scan a trillion raw points per query. The bridge between
> them is **pre-aggregation** — you roll the firehose up into time buckets *as it
> flows*, so the query reads thousands of summarized rows instead of billions of
> raw ones. And the moment you pre-aggregate you hit the trap inside the trap:
> **you cannot average percentiles**. p99 of p99s is meaningless; to roll up a
> percentile you must carry a *mergeable sketch* of the distribution, not the
> number. Wrap all of it in **cardinality** — every distinct combination of tags
> is a new series to track, and a single unbounded tag (a user-id, a URL) can
> explode your in-memory state and melt the store. It's an `INSERT` and a
> `GROUP BY` wrapped in a streaming-aggregation, sketch-math, and flow-control
> problem. That's the rung.

## What it does (the easy part)
- An HTTP (and/or UDP) endpoint that ingests metrics in a **line protocol**
  (`measurement,tag=val,… field=value timestamp`), parses each line into typed
  points, and **publishes them to a durable stream** (the broker) so the write
  path is decoupled from everything downstream.
- A **consumer** that reads the stream, **rolls points up** into fixed time
  windows (1s → 1m → …) per series, and **batch-writes** the rollups into a
  column store (ClickHouse) — never row-by-row.
- A **live dashboard feed**: a `GET /stream` Server-Sent-Events endpoint that
  pushes completed rollup windows to any number of browser clients as they close,
  plus a `GET /query` for historical ranges read back from the column store.
- A `GET /healthz` for liveness.

> The broker (NATS JetStream here; Kafka is the same shape) and ClickHouse are
> **dependencies** — you `cargo add` a client for each. The point of the project
> is everything *between* them: the parse, the rollup engine, the batched
> at-least-once sink, and the backpressured fan-out. Those are the parts a hosted
> metrics platform (Datadog / Prometheus + Cortex / InfluxDB) is really selling.
>
> **Why NATS, not Kafka?** Purely a build-cleanliness call — the canonical Kafka
> client (`rdkafka`) needs a C toolchain (`cmake` + librdkafka) this repo doesn't
> assume, while `async-nats` is pure Rust. JetStream gives you the same
> primitives the verticals reason about: a **durable, replayable log**, a
> **consumer with explicit acks and redelivery**, and **at-least-once** delivery.
> Everything in V3 transfers to Kafka unchanged; the broker is not the lesson.
> (Project 08 — *mini message broker* — is where you build the log itself.)

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The ingest parser + the time-series data model — *what a "metric" actually is*
In `src/parse.rs`, turn a wire line into typed points — and decide the data model
that the rest of the pipeline is built on (`src/model.rs` holds the types).
- Parse a line protocol — `cpu,host=a,region=us usage=0.91,sys=0.12 1719600000`
  — into one [`MetricPoint`](src/model.rs) per field: a **measurement**, a set of
  **tags** (the dimensions you filter/group by), a **value**, and a **timestamp**
  (default to ingest time when absent). Be strict: a malformed line must be
  *rejected and counted*, never allowed to poison the batch downstream.
- The heart of the model is **series identity**. A *series* is a measurement plus
  its exact set of tags; "cpu host=a" and "cpu host=b" are different series. You
  need a stable **fingerprint** (a hash over the measurement + tags **sorted by
  key**, so `a=1,b=2` and `b=2,a=1` collide on purpose) to key every later stage
  by. Get the canonicalization wrong and you either split one series into many or
  merge two — both silently corrupt every graph.
- Stare at **cardinality**: the number of distinct series is the product of every
  tag's distinct values. One unbounded tag (a request-id, a raw URL) turns one
  metric into millions of series and is the classic way to OOM a metrics system.
  Decide where you'd *reject or drop* a high-cardinality series — you don't have
  to enforce it yet, but the model has to make it expressible.

*Concept to internalize:* the metric data model (measurement / tags / value /
timestamp), series identity as a tag-set fingerprint and why canonical ordering
is load-bearing, and cardinality as the cost function of the whole system.

### V2. The rollup engine — *streaming windowed aggregation, and why you can't average a percentile*
In `src/rollup.rs`, build the core: fold the point stream into per-series,
per-window **aggregates** online, and emit each window when it closes. This is the
piece you'd normally get from a stream processor (Flink / Materialize) or a TSDB.
- Bucket each point by `(series_id, window_start)` where `window_start` snaps the
  timestamp down to a fixed **tumbling** window (e.g. 60s). For each bucket keep
  *running* aggregates updated in one pass — `count`, `sum`, `min`, `max`, `last`
  — never a growing list of raw values (that's just deferring the work and the
  memory blow-up).
- **Percentiles are the trap.** A graph wants p50 / p95 / p99, but you cannot
  combine pre-computed percentiles — p99(p99₁, p99₂) is nonsense. To roll a
  percentile up across windows (1m → 5m → 1h) you must carry a **mergeable sketch
  of the distribution**: a fixed-size summary you can `merge` and then query for
  any quantile. Build one yourself — a **bounded histogram** (fixed buckets, the
  HDR/Prometheus approach: cheap, exact-to-bucket-width) or a **t-digest**
  (adaptive, accurate in the tails). Either way it must be *mergeable* and
  *constant-ish space* per series.
- **When does a window close?** Points arrive out of order and late. Pick a
  policy: a **watermark** ("no more points older than `now − grace`") closes and
  flushes a window; points that arrive after their window flushed are *late* —
  decide to drop them (and count it) or re-open. Too eager and you lose late
  data; too lazy and live graphs lag and your in-memory map grows without bound.

*Concept to internalize:* tumbling vs. sliding/hopping windows, online (single-
pass, bounded-memory) aggregation, the percentile-merge problem and why a
mergeable sketch is mandatory, and watermarks / lateness as the flush contract.

### V3. The durable, batched sink — *at-least-once into a column store without melting it*
In `src/sink.rs` (driven by the consumer loop in `src/pipeline.rs`), get rollups
out of memory and into ClickHouse **durably** and **in batches**.
- A column store hates small writes: one `INSERT` per row will fall over long
  before your real throughput. **Micro-batch** — accumulate rollups and flush on
  a **size *or* time** trigger (whichever fires first), so a busy pipeline gets
  big efficient inserts and an idle one still flushes within a bounded delay.
  This batching vs. latency knob is the whole game.
- Make it **at-least-once**. The consumer reads from the durable stream; **ack
  the message only *after* the batch has been durably written** to ClickHouse. A
  crash between write and ack means redelivery — which means **duplicates**, and
  you must have an answer: a dedup/merge key so a re-inserted rollup collapses
  (ClickHouse `ReplacingMergeTree` on `(series_id, window_start)`, or an
  aggregation that's idempotent under replay). There is no free exactly-once.
- **Backpressure.** When ClickHouse is slow, the consumer must *slow down*, not
  buffer the firehose into an OOM. The bounded channel between rollup and sink is
  your backpressure: when it's full, stop pulling from the broker. Reason about
  it explicitly — an unbounded queue in front of a slow sink is a time-bomb.

*Concept to internalize:* micro-batching as the column-store contract (the
size/time flush trigger), at-least-once via ack-after-durable-write, idempotent
writes as the price of at-least-once, and bounded buffers as backpressure.

### V4. The SSE live fan-out — *push completed windows to N dashboards without one slow client stalling the pipeline*
In `src/sse.rs`, serve the live dashboard: every time a window closes, fan the
rollup out to every connected `GET /stream` client over **Server-Sent Events**.
- Implement SSE properly: `text/event-stream`, one `data:` frame per rollup
  (JSON), an `id:` per event, and a `retry:` so a dropped browser reconnects —
  honour `Last-Event-ID` on reconnect so a client can resume. SSE (not WebSocket)
  is the right tool here: the flow is one-directional server→client, it's plain
  HTTP, and it auto-reconnects for free.
- **Fan-out + backpressure.** One source (closed windows) feeds many subscribers.
  A `tokio::sync::broadcast` is the natural hub — but a **slow client** (a backed-
  up browser tab) must never apply backpressure to the *pipeline*. The pipeline's
  liveness cannot depend on the slowest dashboard. So each subscriber gets a
  **bounded** view and a slow one is **conflated or dropped** (broadcast's lagged
  receiver, or keep-latest-per-series), never allowed to block the producer.
  This is the inverse of V3's backpressure and the distinction is the lesson: you
  *must not drop* data headed for durable storage, and you *must be willing to
  drop* data headed for a live view.
- A `GET /query?series=…&from=…&to=…` reads historical rollups back from
  ClickHouse for the panel's initial paint — the SSE stream then keeps it live.

*Concept to internalize:* the SSE protocol (event framing, ids, reconnect /
Last-Event-ID), broadcast fan-out to N subscribers, and load-shedding — why a
live view is allowed to drop while a durable sink is not.

---

## Horizontal checklist (the backend fundamentals)

### Protocols / API
- [ ] Line-protocol ingest over `POST /ingest` (and optionally a UDP listener for
  fire-and-forget StatsD-style senders) — parse, validate, and publish.
- [ ] Server-Sent Events for the live feed (`GET /stream`): correct
  `text/event-stream` framing, event ids, `retry:`, and `Last-Event-ID`
  resume. A `GET /query` for historical ranges.
- [ ] Sensible status codes via the `AppError` → response map: `202 Accepted` on
  ingest (you've durably enqueued, not yet stored), `400` for a malformed
  body or bad query, `404`/`204` for an empty range.
- [ ] Graceful shutdown: stop accepting ingest, **flush the in-flight rollup
  batch to ClickHouse**, ack what you wrote, drain SSE clients, then exit —
  a crash mid-batch is fine (at-least-once covers it), but a clean shutdown
  should not *lose* a partial window it could have flushed.

### State & durability
- [ ] The broker is the durable buffer: a point that's been `202`-accepted
  survives a full restart of the consumer (that's the decoupling the stream
  buys you over an in-process channel).
- [ ] ClickHouse is the queryable source of truth for history; its table is
  partitioned/ordered for the read pattern (by time, then series) and dedups
  replayed rollups (the V3 merge key).
- [ ] Bounded buffers everywhere on the write path — the rollup map, the
  broker→rollup prefetch, and the rollup→sink batch are all capped, and the
  caps are tuned together.

### Security / abuse protection
- [ ] Authenticate ingest (an API key / token) and the query/stream API — an open
  `/ingest` lets anyone forge metrics or blow up your cardinality.
- [ ] Validate and **cap** everything the caller controls: line length, number of
  tags, tag-key/value charset and length, points per request, and a
  **per-tenant cardinality ceiling** (the abuse vector unique to metrics — one
  client with an unbounded tag can DoS the whole store).
- [ ] Never trust a timestamp blindly (reject absurd past/future times that would
  land points in nonsensical partitions); never log raw payloads (tags can
  carry PII).

### Observability
- [ ] The pipeline must observe *itself* (eat your own dog food): gauges for live
  **series cardinality** and **open windows** (in-memory state size — the OOM
  canary), broker **consumer lag** (the are-we-falling-behind metric), and
  **batch fill ratio**.
- [ ] Counters: points ingested / rejected (by reason), windows flushed, rows
  written, duplicates collapsed, SSE clients connected / dropped-for-lag.
- [ ] Histograms: parse time, end-to-end lag (point timestamp → rollup visible in
  ClickHouse) p50/p99, and ClickHouse flush latency. A `tracing` span per
  batch carrying its size and window range.

---

## Cross-cutting scale skills
- Streaming correctness: a *tested* guarantee that a stream of points produces the
  right per-window aggregates — including out-of-order and late arrivals settling
  into the correct (or correctly-dropped) window.
- Sketch math: a percentile sketch that is **mergeable** and stays bounded in
  size, with a measured accuracy bound (error vs. exact quantiles on a known
  distribution).
- Backpressure, both directions: the write path *must not drop* and slows under
  load; the live view *may drop* and sheds load — and you can articulate why each
  is correct.
- Idempotency: an explicit story for the duplicate rollups that at-least-once
  redelivery will hand you, proven by a replay test.

## Definition of done
1. All vertical + horizontal boxes checked.
2. A `bench/` load test (a Rust or `k6`/`vegeta` client that fires a sustained
   line-protocol firehose) reporting: sustained **ingest throughput** (points/sec)
   and end-to-end **lag** p50/p99 (point → queryable) under load; **batched vs.
   row-at-a-time** insert throughput into ClickHouse (the V3 payoff); the rollup
   engine's **sketch accuracy** (measured quantile error vs. exact on a known
   distribution) and its **memory per active series** vs. cardinality (the V2
   payoff); and an **SSE fan-out** run showing many subscribers served while a
   deliberately-stalled client is shed without affecting the others (V4). Numbers
   in `docs/05-benchmarks.md`.
3. A short `docs/05-design.md`: your line-protocol grammar and the series
   fingerprint; the window/watermark policy and which sketch you chose and *why*
   (histogram vs. t-digest, and its accuracy/space tradeoff); the batch flush
   trigger and the at-least-once + dedup design (your ClickHouse engine and sort
   key); and the SSE backpressure/shedding policy.

## Suggested order of attack
1. Get a point in and back out the dumb way: `POST /ingest` parses one line (V1)
   and **publishes to the broker**; a consumer prints what it reads. (Stand up the
   deps first — `docker compose up -d`, then apply the ClickHouse schema.) No
   rollups, no ClickHouse yet — just prove the parse and the durable hop.
2. Build the rollup engine (V2) in memory against a synthetic point stream: snap
   to windows, keep online aggregates, and add the percentile **sketch** with a
   unit test that checks its quantile error. Flush closed windows to a channel.
3. Wire the **batched sink** (V3): consume → rollup → micro-batch → `INSERT` into
   ClickHouse; ack only after the flush. Run a backlog and watch big batches form;
   kill the consumer mid-batch and prove no rollup is lost (and dupes collapse).
4. Add the **SSE live feed** (V4): broadcast each closed window to `GET /stream`;
   open several clients, stall one, and confirm the rest keep flowing. Add
   `GET /query` for the historical paint.
5. Auth ingest, add the validation + **cardinality cap**, then the self-
   observability metrics (cardinality, lag, batch fill).
6. Benchmark (firehose throughput, batched-vs-row insert, sketch accuracy, SSE
   shedding) and write the design doc.

## Run the dependencies
```bash
docker compose up -d        # NATS (JetStream) + ClickHouse
cp .env.example .env        # then fill in values (NATS_URL, CLICKHOUSE_URL, …)

# Apply the ClickHouse schema (also auto-applied by the container's init mount):
#   migrations/0001_init.sql  → mounted at /docker-entrypoint-initdb.d in compose.
# Or by hand:
#   cat migrations/0001_init.sql | docker compose exec -T clickhouse clickhouse-client -mn

# Terminal 1 — the ingest API + (optionally) the consumer pipeline:
cargo run -p metrics-pipeline
#   RUN_CONSUMER=false (default) → ingest API only; the bare scaffold serves
#   cleanly and a POST /ingest panics with the V1 parse todo — that panic is the
#   worklist. GET /stream panics with the V4 todo.
#   RUN_CONSUMER=true             → also spins up the consume→rollup→sink pipeline
#   (panics on the first V2/V3 todo until you implement them).

# Terminal 2 — send a point (line protocol):
curl -X POST localhost:8080/ingest --data-binary \
  'cpu,host=a,region=us usage=0.91,sys=0.12 1719600000'

# Watch the live feed:
curl -N localhost:8080/stream
```
