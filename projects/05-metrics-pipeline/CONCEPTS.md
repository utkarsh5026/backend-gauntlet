# Concept Bank — Project 05: Time-series Metrics Pipeline

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The time-series data model & cardinality *(V1 · `src/parse.rs`, `src/model.rs`)*

**The problem.** "cpu is at 91% on host-a in us-east" needs a shape. Get the shape wrong and every later stage is wrong: if `cpu{host=a,region=us}` and `cpu{region=us,host=a}` hash to different identities, one real series splits into two and every graph shows half the truth. And the shape has a hidden cost function: every distinct tag combination is a new *series* the pipeline must track in memory, forever-ish. One engineer adds `user_id` as a tag and your 500-series system becomes a 5-million-series system by lunch.

**The idea.** A point is measurement + tags (the dimensions you filter/group by) + value + timestamp. A **series** is measurement + its exact tag set, identified by a fingerprint hashed over tags *sorted by key* — canonicalization is load-bearing. Cardinality (the count of distinct series) is the system's true cost metric, and it multiplies: `hosts × regions × endpoints × …`.

**In the wild:** Prometheus's data model is exactly this (its docs warn about cardinality in bold); InfluxDB line protocol (which you're parsing); Datadog's pricing is literally per-series — cardinality is so real it's a billing dimension.

**You own it when you can explain:**
- [ ] Measurement vs tag vs value vs timestamp — and the rule of thumb for what belongs in a tag (bounded, groupable) vs a value/log (unbounded).
- [ ] Series identity: why the fingerprint must be order-canonical, and both corruption modes if it isn't (split series / merged series) — both silent.
- [ ] Cardinality as multiplication: compute the series count for a metric with 4 tags and realistic value counts.
- [ ] The classic cardinality bombs — user id, request id, raw URL, container id — and where a per-tenant ceiling would sit.
- [ ] Why malformed lines are rejected *and counted* — a parse-error metric is how you notice a broken client before it poisons a batch.

**Depth probes:**
- Why do metrics systems store tags once per series (dictionary-encoded) rather than per point? What does that make writes to a *new* series cost vs an existing one?
- A team wants per-customer latency metrics for 50k customers. Tag, or different tool (logs/traces)? Argue it.

**Trap:** treating cardinality as a storage problem. It's a *memory and index* problem — the live series map is RAM, and it's the thing that OOMs first.

---

## 🧠 Card 2 — Windowed aggregation & the percentile trap *(V2 · `src/rollup.rs`)*

**The problem.** A dashboard asks "p99 latency, per service, last 6 hours". Scanning raw points is billions of rows per query — you must pre-aggregate as data flows. Count/sum/min/max fold happily into running values. But percentiles are a trap with teeth: **you cannot average percentiles**. The p99 of two windows' p99s is a meaningless number, and every naive rollup that stores "the p99" per minute produces confidently wrong graphs when it rolls up to hours.

**The idea.** Bucket points into tumbling windows per series, keep single-pass running aggregates, and for percentiles carry a **mergeable sketch** of the distribution — a fixed-size summary (bounded histogram or t-digest) that supports `merge(a, b)` and `quantile(q)`. Mergeability is the property that makes 1m → 5m → 1h rollups mathematically legitimate. Out-of-order arrivals force a **watermark** policy: a window closes when you decide no older points are coming; later points are *late* and get dropped-and-counted or re-opened.

**In the wild:** Prometheus histograms (fixed buckets — you can `sum()` them across instances *because* they're mergeable), HDRHistogram, t-digest in Elasticsearch percentiles, Flink/Beam watermarks for the lateness half.

**You own it when you can explain:**
- [ ] Tumbling vs sliding/hopping windows and how a timestamp snaps to its window start.
- [ ] Why online aggregation must never keep raw value lists — the memory shape of "defer the work".
- [ ] *Why* percentiles don't average — construct a two-window counterexample where avg(p99₁, p99₂) is badly wrong.
- [ ] The mergeability requirement, and the histogram-vs-t-digest tradeoff (fixed buckets: cheap, error = bucket width; t-digest: adaptive, accurate tails, more complex).
- [ ] Watermarks: what "close at now − grace" trades in each direction (drop late data vs lag the live view + unbounded open-window memory).

**Depth probes:**
- Why can Prometheus histograms be aggregated across 500 instances with plain `sum()`? What did the format give up for that (bucket-resolution error)?
- A mobile fleet sends points hours late. What does that do to your watermark policy — and what would a "backfill path" need?

**Trap:** storing computed percentiles per window "to keep it simple". It works until the first rollup or the first cross-host aggregation — then every graph is fiction and nobody notices, because the numbers *look* plausible.

---

## 🧠 Card 3 — The batched, at-least-once sink *(V3 · `src/sink.rs`)*

**The problem.** Column stores (ClickHouse) are built for few, huge inserts; one INSERT per rollup row will fall over long before real throughput. So you batch. But batching interacts with durability: you're consuming from a broker, and if you ack before the batch is written, a crash loses data; if you ack after, a crash between write and ack means the batch is *re-delivered* — duplicates. Pick your poison: at-most-once loses, at-least-once duplicates. (Exactly-once remains a myth here too.)

**The idea.** Micro-batch with a dual trigger — flush at N rows *or* T ms, whichever fires first — so busy pipelines get big inserts and idle ones still flush promptly. Ack only after the durable write (at-least-once), and neutralize the resulting duplicates with a dedup/merge key: ClickHouse `ReplacingMergeTree` on `(series_id, window_start)` makes a replayed rollup collapse into the original. Backpressure closes the loop: when the sink can't keep up, the bounded channel fills, and the consumer *stops pulling from the broker* — the broker holds the backlog durably, instead of your RAM holding it fatally.

**In the wild:** every Kafka→warehouse connector (Kafka Connect sinks batch + ack-after-write), Vector/Fluentd buffering, ClickHouse's own async-insert machinery.

**You own it when you can explain:**
- [ ] Why column stores punish small writes (parts, merges, per-insert overhead) and what the size-or-time trigger optimizes at each traffic level.
- [ ] The ack-placement decision: before vs after the write, and which loss mode each accepts.
- [ ] How the dedup key turns at-least-once into effectively-once *for this data shape* — and why the key must be deterministic from the data.
- [ ] The backpressure chain, link by link: slow ClickHouse → full channel → consumer stops pulling → broker retains → *nothing OOMs*.
- [ ] Why "an unbounded queue in front of a slow sink" is a time-bomb with a fuse length equal to your RAM.

**Depth probes:**
- What does graceful shutdown flush, and why is a crash mid-batch *fine* (redelivery) while a clean shutdown dropping a batch is a bug?
- Where did the broker earn its place — what does NATS/Kafka give here that a `tokio::mpsc` channel between ingest and rollup can't? (Durability across restarts, replay, decoupled deploys.)

**Trap:** tuning batch size for peak traffic only. The *time* trigger exists for the quiet hours — without it, an idle pipeline holds the last few rollups hostage indefinitely.

---

## 🧠 Card 4 — Live fan-out over SSE: the *other* backpressure *(V4 · `src/sse.rs`)*

**The problem.** Dashboards want windows pushed live. One stream of closed windows must reach N browser tabs — and one of those tabs is backgrounded on a laptop that's asleep. If that dead-slow subscriber can apply backpressure to the pipeline, your *durable storage path* now runs at the speed of someone's suspended Chrome tab. The exact mechanism that saved you in V3 would kill you here.

**The idea.** Recognize there are **two backpressure regimes** and data must be classified into one: the durable path must *never drop* (so it slows the producer), the live path must *never slow the producer* (so it drops). For the live view: broadcast fan-out where each subscriber gets a bounded view, and laggards are dropped or conflated. SSE is the right transport: one-directional, plain HTTP, `text/event-stream` frames with event ids, built-in reconnect (`retry:`) and resume (`Last-Event-ID`).

**In the wild:** Grafana Live, GitHub's event streams, LLM token streaming (SSE everywhere), `tokio::sync::broadcast`'s lagged-receiver semantics are exactly this policy encoded in a type.

**You own it when you can explain:**
- [ ] The two-regimes rule and how to classify any given stream (would a gap corrupt state, or just miss a frame the next update replaces?).
- [ ] SSE vs WebSocket: why one-directional push over plain HTTP (proxies, auto-reconnect, simplicity) fits dashboards, and when you'd need the socket instead.
- [ ] The SSE wire mechanics: `data:` frames, `id:`, `retry:`, and how `Last-Event-ID` resumes a dropped connection without a gap (within your buffer).
- [ ] What happens to a lagged broadcast subscriber (skip-ahead + a lag notification) and why that's the correct product behavior for a live chart.
- [ ] Why the historical paint (`GET /query`) + live tail (SSE) split exists — the dashboard's cold-start problem.

**Depth probes:**
- A viewer reconnects after 30 s with `Last-Event-ID`. What can you actually replay, and what bounds it? What do you do when the id is older than your buffer?
- Why is conflation (keep latest per series) strictly better than drop-oldest for a *chart* — and wrong for an *event log*?

**Trap:** letting one code path serve both regimes "for DRY". The policies are opposites; sharing the queue between the sink and the SSE fan-out re-couples what V3/V4 exist to decouple.

---

## ⚡ Rapid-fire round

- [ ] Why ingest returns `202 Accepted` — durably enqueued is not stored, and the client shouldn't wait for ClickHouse.
- [ ] The self-observability canaries: live cardinality (OOM predictor), open windows (watermark health), consumer lag (falling behind), batch fill ratio (batching efficiency) — what each rising line predicts.
- [ ] Why timestamps need sanity bounds on ingest (a point dated 1970 lands in a partition that will never be queried or compacted sanely).
- [ ] Why the pipeline must "eat its own dog food" — a metrics system you can't observe is the last thing to alert on its own death.

## 🔗 Connects to

- The broker-as-durable-buffer role is what you *build* in project 08 (Kafka-lite).
- The two-backpressure-regimes rule reappears in project 03 (live chat drops) vs project 04 (jobs never drop) — this project is where the contrast becomes explicit.
- Mergeable-summary thinking returns in project 20 (BM25 statistics across shards).
