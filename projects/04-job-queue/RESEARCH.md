# Job Queue Technology: Design Principles and the 2026 Landscape

## TL;DR
- A job queue is a **durable, at-least-once work-dispatch system with per-message acknowledgement, redelivery, and lifecycle state** — a narrower thing than a message broker, pub/sub bus, or stream log. The single most important truth in the field: **exactly-once *delivery* is provably impossible** (Two Generals / Akkoyunlu et al. 1975), so every serious design is "at-least-once delivery + idempotent processing = effectively-once," and almost every real mechanism (visibility timeouts, leases, DLQs, transactional outbox) exists to make that equation hold.
- The substrate dictates the design: **Postgres** (`SELECT … FOR UPDATE SKIP LOCKED`) gives transactional enqueue but hits an MVCC dead-tuple wall around tens of thousands of jobs/sec; **Redis** gives sub-millisecond dispatch at 5,000–25,000 jobs/sec per instance but weak durability; **Kafka** is a log, not a queue, and only got real queue semantics in 2025–2026 via KIP-932 share groups; **dedicated brokers** (Pulsar, RabbitMQ quorum, NATS JetStream) and **cloud-managed** services (SQS, Pub/Sub, Service Bus, Cloud Tasks) trade ops burden for guarantees.
- For a Rust engineer building a broker: the ecosystem is real but young (Apalis, sqlxmq, Fang; Iggy.rs, Fluvio), and the frontier design is **thread-per-core + io_uring + shared-nothing** (Iggy's 0.6 rewrite), which trades Tokio's work-stealing convenience for deterministic tail latency. The hard parts are async-trait ergonomics, type-erased job registries, and the durability/latency fsync tradeoff.

---

## Key Findings

1. **"Queue" is a contested word.** A job queue, a message broker, a pub/sub bus, a stream log, and a durable-execution engine occupy overlapping but distinct points in design space. The defining feature of a *job queue* is **per-message acknowledgement with automatic redelivery of un-acked work** plus job lifecycle state (pending → running → done/failed/dead). Kafka historically lacked exactly this — which is why "Kafka as a queue" was always awkward until KIP-932.

2. **Exactly-once delivery is impossible; exactly-once *processing* is achievable.** This is the load-bearing insight. The practical stack is: at-least-once delivery at the transport, idempotent consumers with dedup keys, and the transactional outbox to solve the dual-write problem without two-phase commit.

3. **The "just use Postgres" movement is real and correct — up to a ceiling.** `FOR UPDATE SKIP LOCKED` turns a table into a lock-free competing-consumer queue. It works brilliantly to ~10K–50K jobs/sec, then MVCC dead-tuple accumulation causes the "queue death spiral" first documented by Brandur Leach at Heroku (2015) and reproduced by PlanetScale (2026).

4. **Redis needs care to be reliable.** Naive `BRPOP` loses jobs on crash; the reliable pattern is `BRPOPLPUSH`/`BLMOVE` into a processing list, or Redis Streams with consumer groups (PEL + `XCLAIM`/`XAUTOCLAIM`). Redis 8.4 added single-shot reliable consumption via `XREADGROUP … CLAIM`.

5. **The log-vs-queue impedance mismatch drove a decade of workarounds** (retry topics, parking-lot topics) until KIP-932 share groups shipped GA on Confluent Cloud alongside Apache Kafka 4.2 in early 2026.

6. **Pulsar is the log-based system that got queueing right** via shared/key-shared subscriptions with per-message ack and negative ack — "queues are just subscriptions."

7. **Durable execution (Temporal) is a different abstraction, not a faster queue.** It uses event-sourcing/replay to make multi-step workflows crash-proof; you reach for it when you find yourself building saga state tables and DLQ consumers with business logic inside a queue.

8. **The Rust frontier is thread-per-core + io_uring.** Iggy's migration from Tokio to compio/io_uring is the most instructive current case study for anyone building a broker in Rust.

---

## Details

### Part 1 — First Principles: What a Job Queue Actually Is

**The core problem.** A job queue exists to decouple a producer (which wants to fire-and-forget work) from a consumer (which does the work later), for four reasons: **asynchronous execution** (get the slow work off the request path), **load leveling** (absorb bursts so a spike in enqueues doesn't become a spike in downstream load — the queue is a shock absorber), **backpressure** (queue depth is a signal that consumers are falling behind), and **retries under failure** (work survives worker crashes).

**Taxonomy — where the boundaries blur:**

| Abstraction | Defining trait | Redelivery model | Canonical examples |
|---|---|---|---|
| **Job/task queue** | Per-message ack + lifecycle state; competing consumers | Visibility timeout / lease expiry / PEL | Sidekiq, Celery, SQS, River, Oban |
| **Message broker** | Routing (exchanges, topics) between producers/consumers | Ack/nack per message | RabbitMQ, ActiveMQ |
| **Pub/sub** | Fan-out: every subscriber gets every message | Per-subscription | SNS, Redis Pub/Sub, Google Pub/Sub |
| **Stream log** | Append-only, ordered, offset-based, replayable | Consumer advances offset; no per-message ack | Kafka, Pulsar (storage), Iggy |
| **Durable execution** | Stateful multi-step workflow with replay | Event-history replay; steps not re-run | Temporal, Cadence, Restate, Inngest |

The blur is real: Kafka is a log people use as a queue; Pulsar is a log with true queue subscriptions layered on; Temporal *uses* task queues internally but exposes workflows. A useful heuristic from Inngest: if you can describe the work as a single verb — send, process, sync, generate — it's job-shaped and a queue fits; if it's a multi-step process that must complete as a unit, it's workflow-shaped.

**Core primitives** every job queue implements in some form:
- **enqueue** — append work.
- **dequeue / lease / claim** — take work *without removing it permanently*, so a crash doesn't lose it. This is the crux: reliable queues never destructively pop; they lease.
- **ack / nack** — confirm success (delete) or failure (redeliver).
- **visibility timeout / lease / heartbeat** — the time window a leased job is invisible to other workers; if the worker doesn't ack or extend before it expires, the job is reclaimed. Heartbeats/lease renewal extend the window for long jobs.
- **dead-letter queue (DLQ)** — where poison messages go after N failed deliveries.
- **delayed / scheduled jobs** — run-at-future-time.
- **priorities**, **rate limiting**, **unique/idempotent jobs**, **dependencies/chaining/fan-out**.

### Part 2 — Design Principles and Hard Problems

**Delivery semantics and why exactly-once delivery is impossible.** There are three delivery models: **at-most-once** (fire and forget; ack before processing — lose work on crash), **at-least-once** (ack after processing — duplicate on crash), and the mythical **exactly-once**. The Two Generals Problem proves that over an unreliable network no protocol can guarantee both parties agree with a finite number of messages — any acknowledgement can itself be lost. The formal result is Akkoyunlu et al. 1975. Therefore every "exactly-once" product claim is really **at-least-once delivery + idempotent processing** ("effectively-once" or "exactly-once *semantics*"). Kafka's EOS (idempotent producer + transactions) achieves exactly-once *within* Kafka-to-Kafka pipelines, but it does not extend to external side effects (sending an email, charging a card).

**Idempotency and dedup.** Since duplicates are inevitable, consumers must be idempotent. The canonical technique: a client-generated **idempotency key** (UUID or natural key) stored with a unique constraint in the *same transaction* as the business side effect; on retry, the second insert conflicts and the stored result is returned instead of re-executing. SQS FIFO builds a 5-minute dedup window in at the broker; Pulsar does broker-side dedup via producer sequence IDs; Azure Service Bus offers a configurable duplicate-detection window keyed on MessageId.

**The dual-write problem and the transactional outbox.** You cannot atomically write to a database *and* publish to a broker — there is no shared transaction, and 2PC is a blocking single-point-of-failure you don't want on a hot path. The **outbox pattern** solves this: write the business row and an `outbox` row in one local DB transaction; a separate relay (polling or CDC like Debezium) reads the outbox and publishes to the broker at-least-once; consumers dedup. The guarantee decomposition is elegant: the DB transaction makes business-state and publish-intent atomic; the relay guarantees at-least-once to the broker; consumer idempotency collapses at-least-once to effectively-once. This is why River (Go/Postgres) markets **transactional enqueueing** as its headline feature — because the queue lives *in* your database, enqueue is just part of your business transaction, and the dual-write problem disappears entirely.

**Visibility timeouts vs leases vs heartbeats; reclaiming zombies.** All three solve the same problem: a worker claims a job, then dies. The mechanisms:
- **Visibility timeout** (SQS model): on receive, the message becomes invisible for N seconds (SQS max 12 hours). If not deleted by then, it reappears. Simple, but you must set N > max processing time or you get duplicate processing.
- **Lease + heartbeat** (Postgres-queue idiom): the job row has a `visible_at`/`locked_until` timestamp; the worker updates a `heartbeat_at` every few seconds; a background "reaper" resets jobs whose heartbeat is stale (`UPDATE jobs SET status='pending' WHERE status='running' AND heartbeat_at < now() - interval '30 seconds'`).
- **PEL** (Redis Streams): every delivered-but-unacked message sits in the Pending Entries List with a delivery count and idle time; `XAUTOCLAIM` reclaims entries idle beyond a threshold.

**Ordering vs parallelism — the fundamental tension.** Strict FIFO requires that message N+1 not be processed until N is done, which means **one consumer per ordered stream** — ordering and parallelism are in direct opposition. The universal resolution is **per-key ordering**: partition the stream by a key (user ID, order ID) so ordering holds *within* a key while different keys run in parallel. This is SQS FIFO's `MessageGroupId`, Kafka's partition key, Pulsar's Key_Shared subscription, and Pub/Sub's ordering keys. The failure mode is **head-of-line blocking**: one slow or poison message at the front of an ordered partition stalls everything behind it. It's also the source of the **hot-key** problem: if one key's production rate exceeds a single consumer's processing rate, that key backs up regardless of total capacity.

**Retry strategies.** Best practice is **exponential backoff with jitter** (retry after base·2^n plus randomness) to avoid a synchronized **thundering herd** of retries hammering a recovering downstream. A **retry budget** caps total retries system-wide. **Poison-pill detection** uses a delivery-count: after N deliveries, route to the DLQ rather than retrying forever. Kafka KIP-932 bakes this in with a broker-side delivery-count limit (default 5) after which the record is Archived; NATS JetStream emits a `MAX_DELIVERIES` advisory; RabbitMQ quorum queues track a delivery count and support poison-message handling natively.

**Priority and starvation.** Two implementation approaches: **multiple queues** polled in priority order (Sidekiq's model — check `high` before `default` before `low`; simple but low-priority work can **starve**), or a **single priority-ordered structure** (`ORDER BY priority DESC` in SQL, or a ZSET scored by priority in Redis — BullMQ v5 moved prioritized jobs into a dedicated ZSET). Starvation is mitigated with aging (bump priority as jobs wait) or weighted polling.

**Delayed/scheduled jobs — four implementations:**
- **Sorted set (Redis ZSET)**: score = execution timestamp; `ZRANGEBYSCORE delays -inf now` fetches due jobs; O(log n) insert. This is BullMQ's and most Redis schedulers' approach.
- **Timestamp index (SQL)**: `WHERE run_at <= now()` with an index; a tick-based scanner polls.
- **Timing wheels**: a circular array of buckets, each holding timers due in that tick; O(1) insert/delete. **Hierarchical timing wheels** (Varghese & Lauck 1987) chain wheels of coarsening granularity to cover large ranges — this is how Kafka's request "purgatory" and Netty's `HashedWheelTimer` schedule millions of timers. Kafka's purgatory enqueues *buckets* (not individual tasks) into a `DelayQueue`, capping the priority-queue size at the number of buckets. Best for in-memory, high-churn, short-to-medium delays.
- **Tick-based scanners**: a daemon that wakes periodically (PgQue ticks 10×/sec via pg_cron), giving 50–150ms latency but zero table bloat.

**Backpressure, flow control, and autoscaling.** **Queue depth** and **oldest-message age** are the two golden signals. Depth rising = consumers behind; oldest-age rising = a stuck partition or hot key. Bounded queues apply backpressure by blocking/rejecting enqueues when full. The modern autoscaling pattern is **KEDA** (Kubernetes Event-Driven Autoscaling), which scales worker replicas on queue depth (SQS `ApproximateNumberOfMessages`, Kafka consumer lag, etc.).

**Durability and the fsync tradeoff.** Durability comes from a **write-ahead log** (append-only, sequential writes) plus **replication**. The core tradeoff: **fsync-on-every-write** guarantees no data loss on power failure but caps throughput at disk sync latency; **batched/periodic fsync** is far faster but a crash loses the un-synced window. NATS JetStream, by default, does *not* fsync each write — it uses a `sync_interval` defaulting to 2 minutes, so an OS crash can lose recently-acked messages in a non-replicated setup (replication is the mitigation). Postgres `UNLOGGED` tables skip the WAL entirely for queue tables — much faster, but truncated on crash. This is the durability/latency dial every broker exposes.

**Polling vs long-polling vs push.** Naive polling (`SELECT … every second`) wastes CPU and adds latency. Better: **long polling** (SQS waits up to 20s for a message before returning), **blocking reads** (Redis `BLPOP`/`BRPOPLPUSH`, `BZPOPMIN`), **LISTEN/NOTIFY** (Postgres pushes a notification to waiting workers on insert — Oban uses this, though its Postgres notifier doesn't survive PgBouncer transaction/statement pooling, so Oban ships a PG-based alternative), or **push/streaming** (gRPC streaming, SQS→Lambda, Pub/Sub push subscriptions).

**Observability.** The metrics that matter: queue depth, oldest-message age, processing-latency percentiles (p50/p95/p99), throughput (enqueue vs dequeue rate — the derivative tells you if you're keeping up), retry rate, and DLQ rate. RabbitMQ's `messages_redelivered_total` rate is a classic poison-loop fingerprint.

### Part 3 — Implementation Substrates

#### In-memory / embedded
Go-style channels have Rust analogues: `tokio::sync::mpsc` (async, single-consumer), `crossbeam-channel` (MPMC, plus work-stealing deques for schedulers), and `flume` (MPMC, `Send + Sync + Clone` on both ends, faster than `std::sync::mpsc` and sometimes than crossbeam, no `unsafe`). These are in-process only — no durability, no cross-machine — but they're the right primitive for the internal hand-off between a broker's network threads and its worker pool.

#### Redis-backed
**The reliability ladder:**
- **Naive**: `LPUSH` + `BRPOP`. Blocking, efficient, but `BRPOP` *removes* the job — crash after pop = lost job. This is why Sidekiq's default `basic_fetch` is not crash-safe (Sidekiq makes no exactly-once guarantee and runs jobs *at least once*).
- **Reliable**: `BRPOPLPUSH` (deprecated for `BLMOVE` in Redis 6.2) atomically moves the job from the wait list to a per-worker *processing* list. On success, remove it from processing; on crash, a reaper re-queues stale processing entries. This is Sidekiq Pro's `super_fetch` (using `LMOVE`) and BullMQ's classic design. Cost: `BLMOVE` is per-queue, so M queues × N workers = M·N Redis calls (GitLab found `clientsCronHandleTimeout` from `BRPOP` timeouts consuming 5–11% of Redis CPU at ~5K jobs/s across ~270 queues; raising the timeout from 2s to 5s reclaimed 5–10% of a core).
- **Streams + consumer groups**: `XADD` appends; `XREADGROUP GROUP g c … >` delivers new messages and moves them to the consumer's PEL; `XACK` removes from PEL; `XPENDING` inspects; `XCLAIM`/`XAUTOCLAIM` reclaim idle entries; delivery count enables DLQ routing. This is genuine at-least-once with a visibility-timeout analogue (the `min-idle-time` you pass to `XAUTOCLAIM`). Redis 8.4's `XREADGROUP … CLAIM` collapses the reclaim-then-read loop into one round trip — per Redis's own engineering blog it is "up to 22.5x faster on average on this workload, with a much tighter tail," and a follow-up linked-list optimization added "+28% throughput, –22% average latency, –21% P99 latency, on top of the original 22.5x improvement over XAUTOCLAIM."

**Delayed jobs** use a ZSET scored by timestamp. BullMQ v5 replaced `BRPOPLPUSH` entirely with a Lua `moveToActive` script plus a marker ZSET consumed via `BZPOPMIN`, so workers block on a single key that signals when wait/delayed/prioritized work is ready.

**Durability limitation**: Redis persistence is RDB snapshots + AOF, neither ACID; a crash between job-pop and completion can lose or duplicate work. Redis Streams live in memory (the PEL is a separate radix tree that grows with unacked messages), and single-stream throughput is bounded by one shard. Systems built on Redis: Sidekiq (Ruby), BullMQ (Node), RQ, Resque, Celery-on-Redis.

**Throughput**: Sidekiq's own FAQ states that "on dedicated hardware, Redis should be able to handle anywhere from 5,000 to 25,000 jobs/sec depending on features in use," and separately notes "Customers have reported processing 20,000+ jobs/sec with a single Redis instance"; Redis Streams can sustain 100,000+ `XADD`/sec on a single Redis 7.x instance.

#### Postgres / SQL-backed
**The canonical pattern — `SELECT … FOR UPDATE SKIP LOCKED`** (Postgres 9.5+). Before it, you either used plain `FOR UPDATE` (which serializes workers into a "convoy" — each waits for the row lock ahead) or hand-managed advisory locks. `SKIP LOCKED` changes the lock manager's behaviour: when a row-level lock acquisition would block, instead of waiting, the executor **skips that row and moves to the next** unlocked one. (The row-level lock is skipped; the required `ROW SHARE` table-level lock is still taken normally.) Combined with `LIMIT`, each worker atomically claims a *different* job with no contention:

```sql
WITH next_job AS (
  SELECT id FROM jobs
  WHERE status = 'pending' AND run_at <= now()
  ORDER BY priority DESC, created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs SET status='running', locked_until = now() + interval '60 seconds'
FROM next_job WHERE jobs.id = next_job.id
RETURNING jobs.*;
```

The row lock is tied to the transaction, so a worker crash rolls back and the job reverts to claimable automatically. A partial index (`CREATE INDEX ON jobs (priority DESC, created_at) WHERE status='pending'`) keeps the scan cheap. `SKIP LOCKED` gives an intentionally *inconsistent* view — perfect for work distribution, wrong for accounting.

**Why it breaks down at scale — the MVCC death spiral.** Postgres MVCC means every `UPDATE` and `DELETE` creates a **dead tuple** (old row version), reclaimed only by `VACUUM`. A job queue is pathological churn: every job is inserted, updated (status changes), and deleted. When a long-running transaction (a slow analytics query, an idle-in-transaction connection, a lagging replica with `hot_standby_feedback=on`) holds back the global `xmin` horizon, autovacuum **cannot** reclaim dead tuples. The table and its indexes bloat; the `SKIP LOCKED` scan must walk past ever more invisible tuples; throughput drops; backlog grows; throughput drops more. As thebuild.com (Christophe Pettus) puts it: "This is what Brandur called the 'queue death spiral' at Heroku in 2015 and what PlanetScale hit again in 2026 at 800 jobs/sec while running OLAP on the side." PlanetScale's own writeup confirms the reproduction — "To stress the system enough to produce a death spiral within our 15-minute test window, I increased production to 800 jobs/sec" — and stresses the mechanism is unchanged: "Modern Postgres has raised the threshold — B-tree improvements and SKIP LOCKED buy significant headroom — but the underlying mechanism is unchanged." Mitigations: partitioning (Oban Pro), aggressive autovacuum (pgmq), `UNLOGGED` tables, `TRUNCATE`-based table rotation instead of per-row DELETE (PgQue, built on Skype's PgQ — zero dead tuples by construction, at the cost of 50–150ms latency), or moving the queue off the primary DB. PlanetScale's broader lesson: "the only scalable delete in Postgres is DROP TABLE" — `DELETE` adds work; `TRUNCATE`/`DROP TABLE` produce zero dead tuples.

**Systems**: pgmq (Tembo, SQS-like SQL API, Rust extension via pgrx), River (Go, transactional enqueue, uses `COPY FROM` for batch insert), Oban (Elixir), Que and delayed_job (Ruby), Solid Queue (Rails' default), graphile-worker (Node), Hatchet and Faktory (language-agnostic). **When Postgres-as-queue is right**: you already run Postgres, you want transactional enqueue, and you're under ~10K–50K jobs/sec. **When it breaks**: sustained high churn, OLAP-alongside-OLTP, or when you need fan-out with independent per-consumer cursors (that's PgQ/PgQue territory, not SKIP LOCKED, which is competing-consumers — each job goes to exactly one worker).

**Throughput**: River's own benchmarks report ~46K jobs/sec on an M2 MacBook Air with 2,000 worker goroutines in burn-down mode (River cautions against treating this as gospel); realistic competing-consumer Postgres queues land ~10K–15K jobs/sec under load; RudderStack scaled a Postgres queue to 100,000 events/sec but "only after we successfully navigated challenges like table bloat, query performance degradation, index bottlenecks, and retry storms" — via partitioning into 100K-row datasets, `COPY`-based batch inserts, and careful indexing.

#### Kafka / log-backed
**Why a log is not a queue.** Kafka is a partitioned, append-only, offset-ordered log. Classic consumer groups enforce **one consumer per partition** — so parallelism is capped by partition count (forcing "over-partitioning" for peak load), there is **no per-message ack** (only offset commits — you advance past a batch), **no arbitrary redelivery** of a single failed message, **no per-message delay**, and a slow message causes **head-of-line blocking** of its whole partition. None of these match job-queue semantics.

**The workarounds** people built: **retry topics** (on failure, publish to `topic-retry-5m`, `topic-retry-30m`, consumed after a delay), **parking-lot / DLQ topics** for poison messages (the Uber and Confluent patterns), and external schedulers for delays.

**KIP-932 "Queues for Kafka" — share groups.** This is the big 2025–2026 development. A **share group** allows *multiple* consumers to cooperatively consume the *same* partition concurrently, with **per-message acknowledgement** and **broker-tracked delivery counts** via time-limited acquisition locks (a visibility-timeout analogue). Consumer parallelism is decoupled from partition count — you can have 50 consumers on a single-partition topic. Acknowledgement types are ACCEPT/RELEASE/REJECT; a delivery-count limit (`group.share.delivery.count.limit`, default 5) archives poison messages. Ordering is *not* guaranteed within a share group (messages can be processed out of offset order). Status as of 2026: Early Access in Kafka 4.0, Preview in 4.1, and **GA on Confluent Cloud coinciding with Apache Kafka 4.2** (early 2026); it ships in Confluent Platform 8.2. DLQ support (KIP-1191, targeting ~4.4), exponential backoff, exactly-once via transactions, and key-based ordering are on the near-term roadmap.

#### Dedicated brokers
- **RabbitMQ**: AMQP with exchanges (direct/topic/fanout/headers) routing to queues. **Classic queues** (non-replicated since 4.0, mirroring removed) are fastest for high-churn but offer low data safety. **Quorum queues** (Raft-replicated) are the durable default: poison-message handling, delivery-count limits, at-least-once dead-lettering, delayed retry. **Streams** (3.9+) are an append-only, replayable, non-destructive-read log for fan-out and replay. **Delayed messages** come from the community plugin (scoped to seconds/minutes/hours — "a day or two at most"; scheduler state historically in Mnesia on one node — can lose messages on failure before due time) or TTL+DLX. Throughput: per RabbitMQ's own migration blog, "A quorum queue can sustain a 30000 msg/s throughput (again, using 1kb messages)… Meanwhile, classic mirrored queues offer only a third of that throughput," i.e. ~10,000 msg/s; RabbitMQ excels at low latency (~1ms) at moderate throughput.
- **NATS JetStream**: streams with retention policies — **`WorkQueuePolicy`** deletes each message once acked (true work-queue; consumers must have disjoint filter subjects), `LimitsPolicy`, `InterestPolicy`. Consumers are **pull** (recommended for work queues, `AckExplicit`) or push; `AckWait` is the visibility timeout, `MaxDeliver` caps retries, `Nak`/`Term`/`InProgress` are the nack/dead-letter/heartbeat primitives; `MAX_DELIVERIES` advisories enable DLQ. Default fsync is periodic (`sync_interval` ~2 min), so durability leans on replication. Core NATS request-reply round-trip latency is ~50 µs per NATS's own bench tool.
- **Apache Pulsar**: often cited as the best fit for queueing on a log, because "queues are just subscriptions." **Shared** subscriptions round-robin messages across competing consumers with per-message ack; **Key_Shared** preserves per-key ordering while sharing; **Failover** and **Exclusive** cover ordered/single-consumer cases. Pulsar has per-message individual ack, **negative ack**, `RedeliveryBackoff`, broker-side dedup, delayed delivery (shared/key-shared only, via `DelayedDeliveryTracker`), and transactions for end-to-end EOS. Caveats: Key_Shared has had real bugs (stuck consumers with unacked messages pre-4.0; nack support for Key_Shared only arrived with PIP-379 in 4.0). Throughput ~305 MB/s per 3-node cluster in Confluent's (Kafka-vendor) benchmark; StreamNative (Pulsar-vendor) disputes the methodology and shows Pulsar matching Kafka on latency, especially on catch-up reads.
- **Others**: ActiveMQ/Artemis (JMS), Beanstalkd (simple, fast, `tube`-based with built-in delay and TTR), Gearman (venerable, minimal).

#### Cloud-managed
- **AWS SQS**: **Standard** = nearly unlimited throughput ("unlimited queues and messages"), at-least-once, best-effort ordering. **FIFO** = strict per-`MessageGroupId` ordering, exactly-once via a **5-minute dedup window** (content-based SHA-256 or explicit `MessageDeduplicationId`), 300 msg/s per API (3,000 batched), with high-throughput mode much higher. Visibility timeout 0s–12h; long polling up to 20s; delay queues/message timers up to 15 min; DLQ via `maxReceiveCount` redrive; 256KB message limit (S3 pointer for larger). SQS peaked at tens of millions of messages/sec during Amazon Prime Day (a widely cited AWS figure was 47.7M msg/s at Prime Day 2021, with later years higher). **Amazon MQ** (managed ActiveMQ/RabbitMQ), **EventBridge** (event bus + scheduler), **Step Functions** (durable-execution-style orchestration).
- **Google Cloud Pub/Sub**: at-least-once by default; **exactly-once delivery** (pull subscriptions, single region) via a persistence layer tracking delivery state — ack IDs expire and only the latest is valid; **ordering keys** for per-key order (ordering+exactly-once caps throughput to ~thousands msg/s). Ack deadline is the visibility timeout; push and pull modes; DLQ via dead-letter topic. **Cloud Tasks** is the more job-queue-like service: explicit HTTP target per task, at-least-once, per-queue **rate limiting** (up to 500 dispatches/s) and **max concurrent dispatches** (default 1,000), exponential-backoff retries, and **scheduling up to 30 days** out — use it when each job goes to one endpoint and you need pacing/scheduling.
- **Azure**: **Storage Queues** (simple, 64KB messages, up to 200GB via blob, visibility timeout, no FIFO guarantee, DIY dead-letter via dequeue count) vs **Service Bus** (enterprise: **sessions** for FIFO groups, **scheduled messages**, **duplicate detection** window on MessageId, native **dead-letter subqueue** with reasons, `MaxDeliveryCount` default 10, transactions, autoforwarding, 256KB/1MB/100MB by tier).
- **Cloudflare Queues**, **Upstash QStash** (HTTP-based, serverless), **Inngest** and **Trigger.dev** (durable-execution-flavored serverless job platforms).

### Part 4 — The Rust Ecosystem (deep dive)

**Job-queue libraries:**
- **Apalis** — the most prominent; functional/DI design (no macros), Tokio-native, integrates with Axum/Actix, multiple backends (Redis, Postgres, SQLite, MySQL), middleware for retries/graceful-shutdown. `WorkerBuilder::new(...).backend(b).build(handler)`.
- **sqlxmq** — Postgres-backed via sqlx; `#[job]` attribute macro, `JobRegistry`, `LISTEN/NOTIFY`-driven, concurrency limits (`set_concurrency(10, 20)`).
- **Fang** — blocking (threaded) and async (Tokio) workers, Postgres/SQLite/MySQL backends, cron/scheduled/unique tasks, retries with custom backoff, single-purpose typed workers; panicked workers auto-restart.
- **Underway**, **Ironworker**, **Aide-de-camp**, **OcyPod** (Redis-backed HTTP job server), **rusty-celery** (Celery protocol in Rust), **deadqueue**, plus raw **Tokio mpsc / crossbeam / flume** for in-process.
- **Faktory-rs** — Rust client for Faktory (Mike Perham's language-agnostic job server, the Sidekiq-in-Go-with-any-client model).

**Rust-written brokers/streaming:**
- **Iggy.rs** (Apache Iggy, incubating) — persistent message-streaming log built from scratch: Stream→Topic→Partition→Segment, append-only log, QUIC/TCP/WebSocket/HTTP, zero-copy custom (de)serialization, consumer groups. **The 0.6.0 rewrite (Dec 2025) moved from Tokio's work-stealing executor to a thread-per-core, shared-nothing, io_uring/compio completion model** — each core is a pinned, NUMA-aware shard with no hot-path locks and no GC. Official claims: millions of msg/sec, a target of 5M × 1KB msg/s, >5,000 MB/s, sub-ms p99 (the widely-repeated "20 million msg/s via TCP" figure is secondary reporting of unnamed adopters, not a reproducible published benchmark). Clustering (Viewstamped Replication) is in progress. Accepted to the Apache Incubator February 2025.
- **Fluvio** (InfinyOn) — mature Rust+WASM streaming platform (5+ years), aims at Kafka+Flink territory with in-stream WASM processing.
- **Redpanda** (C++, not Rust, but the key comparison) — thread-per-core Kafka-compatible broker; the architectural template Iggy echoes.
- **Clients**: `rdkafka` (librdkafka wrapper), `async-nats`, `kafka-rust`.

**What Rust brings — and what's hard.** Rust's wins for this domain: **no GC pauses** (predictable tail latency — the whole point of a broker), **zero-cost async**, small memory footprint, and safe concurrency. The frontier techniques: **thread-per-core** (pin one runtime per core, shard data by core, eliminate cross-thread synchronization and cache-line bouncing) vs Tokio's **work-stealing** (easier load balancing, but less control and non-deterministic scheduling); **io_uring** (completion-based async I/O via shared submission/completion ring buffers — a poor fit for Rust's poll-based `Future` model, bridged by submitting the request on the Future's first poll, via `compio`/`glommio`/`monoio`) vs Tokio's epoll/readiness model; and **zero-copy** serialization working directly on binary buffers.

The genuinely hard parts in Rust specifically:
- **Async trait ergonomics** — job handlers as `async fn` in traits historically needed `async-trait` (boxing/dynamic dispatch); native AFIT helps but dyn-compatibility for a heterogeneous handler registry is still awkward.
- **Type-erased job registries** — dispatching a serialized payload to the right typed handler means erasing types (often `serde_json::Value` → downcast, or a registry of `Box<dyn Fn>` keyed by a stable job "kind" string, as River does).
- **Payload serialization** — every job crosses a serde boundary; you choose JSON (debuggable, slow) vs bincode/protobuf/custom (fast, opaque).
- **Interior mutability across `.await`** — Iggy hit `RefCell already borrowed` panics from holding borrows across await points (the `clippy::await_holding_refcell_ref` footgun), a real hazard in a shared-nothing design; they note GhostCell-style statically-checked borrowing across Futures is possible but not yet ergonomic.

**Concrete guidance for building a broker/queue in Rust:**
1. **Storage engine**: an append-only segmented WAL (Iggy/Kafka model) — fixed-size segment files, offset-indexed. Decide your fsync policy explicitly (per-write for durability, batched for throughput) and make it configurable; consider group commit (batch many enqueues into one fsync).
2. **io_uring vs Tokio**: default to Tokio unless you have measured tail-latency requirements that justify thread-per-core; the ecosystem (Axum, sqlx, most crates) assumes Tokio. If you go thread-per-core, budget for the ergonomic cost (compio/monoio, sharding logic) — but gain deterministic-testing benefits.
3. **Thread-per-core vs work-stealing**: thread-per-core shines when you can cleanly shard by key (partitions) and want predictable p99; work-stealing shines for heterogeneous, bursty, uneven work.
4. **Protocol**: a custom binary TCP protocol beats HTTP/JSON for the hot path (Iggy found raw TCP faster than QUIC in its benchmarks); offer HTTP as a convenience layer.
5. **Zero-copy**: work on `bytes::Bytes` / borrowed buffers; avoid deserializing payloads the broker never inspects.

### Part 5 — Comparison and Selection

**Comparison matrix** (throughput figures are order-of-magnitude and condition-dependent; see caveats below):

| System | Durability | Delivery | Ordering | Throughput (indicative) | Delayed | Priority | DLQ | Ops burden |
|---|---|---|---|---|---|---|---|---|
| **Postgres (River/Oban/pgmq)** | Strong (ACID, WAL) | At-least-once | Per-query/insert order | ~10K–50K jobs/s | Yes (`run_at`) | Yes (`ORDER BY`) | Yes | Low (reuse DB) |
| **Redis (Sidekiq/BullMQ)** | Weak (RDB/AOF) | At-least-once (reliable fetch) | FIFO-ish per list | 5K–25K jobs/s; Streams 100K+ XADD/s | Yes (ZSET) | Yes (multi-queue/ZSET) | Yes | Low–medium |
| **Kafka (share groups)** | Strong (replicated log) | At-least-once | Per-partition (not within share group) | ~600K msg/s per small cluster; millions at scale | Weak (retry topics) | No | KIP-1191 (roadmap) | High |
| **RabbitMQ quorum** | Strong (Raft) | At-least-once | Per-queue FIFO | ~30K msg/s (3-node, 1KB) | Plugin / TTL+DLX | Yes | Native | Medium |
| **NATS JetStream** | Configurable (fsync interval) | At-least-once (+ publish dedup) | Per-subject | High (100K+ range) | Limited | No | Advisory-based | Medium |
| **Pulsar** | Strong (BookKeeper) | At-least-once + EOS txn | Per-key (Key_Shared) | ~305 MB/s (3-node) | Yes (shared/key-shared) | No | Native | High |
| **SQS Standard** | Strong (managed) | At-least-once | None | Nearly unlimited | 15 min max | No | Native (redrive) | None (managed) |
| **SQS FIFO** | Strong | Exactly-once (5-min dedup) | Per-MessageGroupId | 300/s→3K batched→HT mode | 15 min | No | Native | None |
| **Google Pub/Sub** | Strong | At-least-once / EOS (pull, region) | Ordering keys | Very high | No (use Cloud Tasks) | No | Dead-letter topic | None |
| **Cloud Tasks** | Strong | At-least-once | No | Rate-limited (≤500/s/queue) | 30 days | No | Yes | None |
| **Azure Service Bus** | Strong | At-least-once (dedup window) | Sessions (FIFO) | High | Scheduled msgs | No | Native subqueue | None |
| **Temporal (durable exec)** | Strong (event history) | Effectively-once steps | Per-workflow | Orchestration, not raw throughput | Native (timers) | Task-queue priority | Via workflow logic | High (self-host) or managed |

**Throughput caveats**: These numbers come from heterogeneous sources measured under different conditions (message size, hardware, batching, replication). The Kafka/Pulsar/RabbitMQ MB/s figures are from Confluent's OpenMessaging benchmark (Kafka ~605 MB/s, Pulsar ~305 MB/s, RabbitMQ ~38 MB/s, on 3× i3en.2xlarge with 1KB messages) — but Confluent is the Kafka vendor and StreamNative (Pulsar vendor) disputes the methodology. Postgres and Redis jobs/sec depend heavily on job size and feature set. Iggy's figures are largely self-reported. Always benchmark your own workload.

**Decision framework:**
- **Already on Postgres, <10K jobs/sec, want transactional enqueue** → Postgres queue (River/Oban/pgmq). Simplest, no new infra, dual-write problem gone.
- **Need sub-ms dispatch, high throughput, can tolerate rare loss** → Redis (Sidekiq/BullMQ) with reliable fetch.
- **Fully managed, don't want to run anything** → SQS (Standard for throughput, FIFO for ordering), Pub/Sub, or Service Bus. Cloud Tasks specifically when you need per-job HTTP dispatch with rate limiting/scheduling.
- **Need queue semantics AND you already run Kafka for streaming** → KIP-932 share groups (consolidate the estate).
- **Need true competing-consumer queues with per-message ack on a durable log, per-key ordering, negative ack, delays** → Pulsar.
- **Enterprise routing, moderate throughput, low latency, rich features** → RabbitMQ quorum queues.
- **Cloud-native, lightweight, edge, or already in the NATS ecosystem** → NATS JetStream work queues.
- **Multi-step, long-running, stateful workflows where completion must be guaranteed** (payments, onboarding, AI agent pipelines) → durable execution (Temporal/Restate/Inngest/Hatchet), *not* a queue. The tell: you're building saga state tables, custom retry orchestration, or DLQ consumers with business logic.

**Production war stories worth internalizing:**
- **Brandur/Heroku (2015)** and **PlanetScale (2026)**: the Postgres MVCC death spiral — a large backlog within an hour / a 15-minute death spiral at 800 jobs/sec with OLAP alongside. Lesson: monitor dead-tuple ratio, keep long transactions off the queue DB, partition or rotate tables.
- **GitLab/Sidekiq**: `BRPOP` timeout tuning reclaimed 5–10% of a Redis core at ~5K jobs/s across ~270 queues. Lesson: at scale, the queue's own polling overhead becomes the bottleneck; specialize workers to fewer queues.
- **RudderStack**: scaled Postgres to 100K events/sec only via partitioning into 100K-row datasets, `COPY`-based batch inserts, and careful indexing. Lesson: Postgres-as-queue scales far, but not for free.
- **Pulsar Key_Shared bugs**: stuck consumers and "ack-hole" issues under backlog. Lesson: even the "best" queueing-on-a-log system has sharp edges in its most advanced mode.

### Part 6 — Current State (2025–2026)

- **KIP-932 Queues for Kafka** went from Early Access (4.0) to Preview (4.1) to GA on Confluent Cloud alongside Apache Kafka 4.2 in early 2026 — the most significant queueing development of the period. DLQ support (KIP-1191, ~4.4), exactly-once via transactions, exponential backoff, and key-based ordering are next.
- **"Just use Postgres" consolidated** as mainstream advice, with a maturing toolbelt (River, pgmq, Oban Pro partitioning, graphile-worker, Solid Queue as Rails' default) — *and* a maturing understanding of its limits, crystallized by PlanetScale's 2026 death-spiral reproduction and the emergence of bloat-free designs like PgQue (Skype PgQ lineage, TRUNCATE rotation, ~50–150ms latency).
- **Durable execution gained major ground**: per Temporal's own announcement (Feb 17, 2026), the company raised "$300 million Series D financing at a $5 billion valuation," led by Andreessen Horowitz, on ">380% year-over-year revenue growth" and "9.1 trillion lifetime action executions on their Cloud product alone, 1.86 trillion for AI-native companies" (GeekWire notes this doubled the company's valuation from $2.5B in October). Alongside Temporal sit Restate, Inngest, Hatchet, and Conductor — the surge driven substantially by **AI agent workloads** (long-running, multi-step, human-in-the-loop), which are inherently workflow-shaped, not job-shaped.
- **Serverless queues** matured (Cloudflare Queues, Upstash QStash, SQS→Lambda, Pub/Sub push).
- **Rust projects advanced**: Apache Iggy's io_uring/thread-per-core rewrite (0.6.0, Dec 2025) and Apache incubation (Feb 2025); Fluvio's continued development; Redis 8.4's `XREADGROUP … CLAIM`.

---

## Recommendations

**Staged, concrete next steps — for someone building a broker/queue in Rust:**

1. **Fix your delivery-semantics contract on paper first.** Commit to at-least-once delivery + idempotent processing. Design the ack/lease/redelivery protocol first (visibility timeout vs explicit heartbeat lease), because it dictates your storage schema. Threshold to revisit: if you need fewer than ~1 duplicate per 10⁶ under crash testing, add broker-side dedup (producer sequence IDs) like Pulsar.

2. **Prototype on Postgres or Redis before writing a broker.** If your target throughput is <10K jobs/sec and you want transactional enqueue, `SELECT … FOR UPDATE SKIP LOCKED` (or Apalis/sqlxmq/Fang) may be all you ever need — most systems never outgrow it. Only build a bespoke broker if you have a measured requirement (tail latency, throughput, or protocol) that existing systems provably can't meet.

3. **If you build the broker, make these decisions explicitly and in this order:** (a) WAL segment format + fsync policy (start batched/group-commit, expose per-write as an option); (b) Tokio first, thread-per-core (compio/monoio) only if p99 tail latency is a hard, measured requirement — you lose ecosystem compatibility; (c) custom binary TCP protocol for the hot path; (d) a `kind`-string → typed-handler registry to sidestep async-trait/type-erasure pain; (e) `bytes::Bytes` zero-copy for payloads the broker doesn't inspect.

4. **Instrument from day one**: queue depth, oldest-message age, enqueue-vs-dequeue rate, p50/p95/p99 processing latency, retry rate, DLQ rate. These are the signals that turn a 3AM outage into a dashboard glance.

5. **Study Iggy's source and its migration blog directly** — its Tokio→io_uring writeup is the single most relevant document for your exact task, including the concrete Rust footguns (RefCell-across-await, poll-vs-completion impedance mismatch).

**Thresholds that change the recommendation:**
- Crossing ~10K–50K sustained jobs/sec on Postgres, or seeing dead-tuple ratio climb week-over-week → move the queue off the primary DB, partition, or switch to Redis/dedicated broker.
- Needing cross-region delivery, fan-out with independent per-consumer cursors, or weeks of retention → not a job queue; use Kafka/Pulsar (streaming) or a PgQ-style event log.
- Finding yourself building saga state tables, multi-step retry orchestration, or DLQ consumers with business logic → adopt durable execution (Temporal/Restate/Hatchet) rather than extending your queue.

---

## Caveats

- **Throughput numbers are not comparable across rows** of the matrix: they come from different sources, message sizes, hardware, batching, and replication settings. Vendor benchmarks (Confluent for Kafka, StreamNative for Pulsar, BullMQ for BullMQ-vs-Oban) favor their own products; treat all figures as order-of-magnitude and benchmark your own workload.
- **The Iggy "20 million msg/s via TCP" figure is secondary reporting** of unnamed early adopters, not a reproducible published benchmark; the defensible official claim is "millions of messages per second" / a 5M×1KB-msg/s target with >5,000 MB/s and sub-ms p99.
- **"Exactly-once" everywhere means "exactly-once semantics/processing," never exactly-once delivery.** SQS FIFO, Pub/Sub, and Kafka EOS all provide bounded dedup *within their own boundaries*, not end-to-end exactly-once side effects.
- **Fast-moving area**: KIP-932 client/cluster support, Iggy clustering (Viewstamped Replication), and durable-execution valuations are all in flux as of mid-2026; verify current status before committing.
- Some supporting figures (certain third-party benchmark blogs, secondary reporting of valuations) come from non-primary sources; primary docs and first-party engineering blogs were prioritized for all mechanism-level claims, and vendor/secondary origins are flagged inline.
