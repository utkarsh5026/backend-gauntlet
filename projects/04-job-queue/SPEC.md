

# Project 04 — Distributed Job Queue

> "Put a job on a queue, have a worker run it later." It sounds like a wrapper
> around a database table — `INSERT` a row, `SELECT` it back, run it. The trap is
> everything that the words *distributed*, *durable*, and *at-least-once* smuggle
> in. The moment you run **more than one worker**, two of them will `SELECT` the
> same row and run the job twice — unless the dequeue is a single atomic step that
> hands each job to exactly one worker (`SKIP LOCKED`). The moment a worker
> **crashes mid-job**, that job is neither done nor available to anyone else; it's
> stuck — unless every claim is a *lease* that expires and returns the job to the
> pool (**visibility timeout**). The moment a job **fails**, naively retrying it
> forever turns one poison message into an infinite hot loop — unless retries
> back off and give up into a **dead-letter queue**. And a worker that **polls a
> empty table in a tight loop** melts the database, while one that polls slowly
> adds latency to every job. It's a database table wrapped in a hard concurrency,
> failure-recovery, and flow-control problem. That's the rung.



## What it does (the easy part)

- An HTTP API to **enqueue** a job (`POST /jobs` with a `queue`, a `kind`, and a
JSON `payload`) and to **inspect** one (`GET /jobs/{id}`).
- A pool of in-process **workers** that claim ready jobs from Postgres, run them,
and mark them done — multiple workers in one process, and multiple *processes*
against the same database, without ever running a job twice concurrently.
- **At-least-once** delivery: a job claimed by a worker that dies is automatically
retried by another worker once its lease expires.
- **Retries with backoff** for jobs that fail, and a **dead-letter queue** for
jobs that exhaust their attempts.
- **Delayed / scheduled** jobs (run no earlier than `run_at`).
- A `GET /healthz` for liveness.

> Postgres here is doing double duty: it is both the **durable store** *and* the
> **queue broker**. The whole point of the project is that the queue semantics —
> the parts you'd normally get from RabbitMQ / SQS / Sidekiq — are things you
> build on top of plain SQL rows.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it. The criteria describe *what the system must do*, never *how*;
> figuring out the how is the point. A box only flips to ✅ when its Proof exists.

---



## Vertical challenges (build these yourself — this is the learning)



### V1. The claim engine — *the SKIP LOCKED dequeue, from scratch*

In `src/queue.rs`, build `enqueue` (an `INSERT`) and the heart of the system:
`claim`, the atomic dequeue. This is the thing you'd normally get from a broker.

- The naive `SELECT id FROM jobs WHERE state='ready' LIMIT 1` then `UPDATE`
**double-dispatches**: two workers read the same row before either updates it.
The fix is to make selecting *and* claiming one atomic statement —
`SELECT ... FOR UPDATE SKIP LOCKED` inside the same transaction (or a single
`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED ...) RETURNING *`).
`FOR UPDATE` row-locks the candidates; `SKIP LOCKED` makes a *second* worker
step over the rows the first already locked instead of blocking on them.
- Claim a **batch** (`LIMIT n`), not one row — a worker that round-trips to the
DB per job spends all its time on latency, not work.
- The claim must respect ordering (`run_at <= now()`, oldest first) and the
per-queue selector. Think about the **index** that keeps this `SELECT` cheap as
the table grows to millions of rows (a partial index over the `ready` rows is a
strong start) — and prove it with the bench.

**Done when ALL true:**

- [x] `enqueue` inserts; `claim` selects **and** locks in one atomic statement (`FOR UPDATE SKIP LOCKED`).
- [x] **Two+ workers never claim the same row** — a second worker steps over locked rows instead of blocking on them.
- [x] `claim` takes a **batch** (`LIMIT n`) in one round-trip, and respects `run_at <= now()`, oldest-first, and the per-queue selector.
- [x] A **partial index over the** `ready` **rows** keeps the claim `SELECT` cheap as the table grows — shown with-vs-without in the bench.

**Proof:** a concurrency test (N workers, shared backlog) asserting no double-claim; an `EXPLAIN`/bench showing the index payoff in `docs/04-benchmarks.md`.

*Concept to internalize:* a relational table as a concurrent queue, why the
read-then-write race is the central bug, and how `FOR UPDATE SKIP LOCKED` turns
row locks into a contention-free hand-off.

### V2. Visibility timeout — *leases, and what "at-least-once" really costs*

A claim is not "this job is done" — it's "this worker is *allowed to try* for a
while." In `src/lease.rs`, make every claim a **lease** and reclaim dead ones.

- When `claim` takes a job it stamps `locked_by` / `locked_until = now() + lease`
and flips it to `running` — so no other worker can see it. On success the worker
**acks** (→ `done`); a crash before the ack leaves it `running` with an expired
lease.
- Build the **reaper**: a periodic sweep that finds `running` jobs whose
`locked_until` has passed and returns them to `ready`. That sweep is the entire
reason a crashed worker doesn't lose a job.
- This buys **at-least-once**, and you must stare directly at the consequence: a
worker can finish a job and die *before* acking, so the job runs again. There is
no free exactly-once — the honest answer is **idempotent handlers** (or an
idempotency key). Reason about it explicitly; the lease length is a real
tradeoff (too short → spurious double-runs of slow jobs; too long → slow
recovery from a crash). Wiring a lease *heartbeat* for long jobs is the stretch.

**Done when ALL true:**

- [x] A claim stamps `locked_by` / `locked_until` and flips the job to `running`; a successful worker **acks** it to `done`.
- [x] A **reaper** returns `running` jobs whose lease has expired to `ready` — a crashed worker loses nothing.
- [x] A worker killed mid-job has its job **picked up by another** once the lease expires (chaos-tested).
- [x] The at-least-once consequence is faced explicitly: handlers are **idempotent** (or use an idempotency key), documented in `docs/04-design.md`.

**Proof:** a chaos test that drops a worker mid-job and asserts the job still completes via another worker; design-doc note on lease length + idempotency.

*Concept to internalize:* the lease / visibility-timeout pattern, at-least-once
vs. at-most-once vs. the myth of exactly-once, and idempotency as the thing that
makes at-least-once safe.

### V3. Retries with backoff + the dead-letter queue — *failure as a first-class state*

Jobs fail. In `src/retry.rs`, decide what happens on failure so one bad job can't
take the system down.

- On failure, increment `attempts`, record `last_error`, and — if attempts
remain — reschedule the job for the future (`run_at = now() + backoff`,
state back to `ready`). When attempts are exhausted, move it to the
**dead-letter queue** (a terminal `dead` state, or a separate table) instead of
retrying forever.
- The backoff must be **exponential with jitter**, capped at a maximum. Fixed
retries synchronise every failing worker into a thundering herd; exponential
*without* jitter still does (everyone retries at exactly `2^n`). Jitter spreads
them out. (You'll want a small RNG — add `rand` to the workspace deps when you
get here.)
- A **poison message** — one that fails every time — is the case the DLQ exists
for: it must land in the DLQ and stop, not loop. Make the DLQ inspectable; a
dead job you can't see or requeue is a silent data-loss bug.

**Done when ALL true:**

- [x] On failure: `attempts` is incremented, `last_error` recorded, and if attempts remain the job is rescheduled (`run_at = now() + backoff`, state → `ready`).
- [x] Backoff is **exponential with jitter, capped** at a maximum — not fixed, not bare `2^n`.
- [x] A **poison message** (fails every time) lands in the **DLQ** and **stops** — it never loops forever.
- [x] The DLQ is **inspectable and requeueable** — a dead job you can't see or requeue is silent data loss.

**Proof:** a test that enqueues an always-failing job and asserts it backs off, then lands in the DLQ (not a hot loop); the backoff curve recorded in the design doc.

*Concept to internalize:* retry policy as a contract, exponential backoff with
jitter (and *why* the jitter), poison messages, and the dead-letter queue as the
release valve that keeps one bad job from becoming an outage.

### V4. Scheduling + LISTEN/NOTIFY — *low latency without busy-polling*

Delayed jobs already "work" through V1 (the claim filters `run_at <= now()`), so a
worker that polls every second will eventually pick them up. In `src/scheduler.rs`,
remove the latency-vs-load tradeoff that polling forces on you.

- **Polling** is the floor: a worker that `SELECT`s every *N* ms adds up to *N* ms
of latency to every job and runs a flood of empty queries against an idle DB.
Make *N* small and you hammer Postgres; make it large and jobs sit waiting.
- Build the wakeup path with Postgres `LISTEN` **/** `NOTIFY`: `enqueue` (and a
retry/delay coming due) issues a `NOTIFY` on the queue's channel; idle workers
`LISTEN` and wake the instant work appears — falling back to a slow poll so a
missed notification (or a job whose `run_at` is in the future) is never stranded
forever. The result is **millisecond pickup latency *and* near-zero load on an
idle queue**.
- Reason about the failure mode: `NOTIFY` is fire-and-forget and not durable, so
the poll fallback is not optional — it's what makes the notify an *optimization*
rather than a correctness dependency.

**Done when ALL true:**

- [ ] `enqueue` (and a delay/retry coming due) issues a `NOTIFY` on the queue's channel; idle workers `LISTEN` and wake on it.
- [x] A **slow poll fallback** remains, so a missed `NOTIFY` or a future `run_at` is never stranded forever.
- [ ] Pickup latency on an otherwise-idle queue is **milliseconds**, with **near-zero load** when idle — shown poll-vs-NOTIFY in the bench.
- [ ] The durable poll path stays the **source of truth**; `NOTIFY` is an optimization, not a correctness dependency (reasoned in the design doc).

**Proof:** a poll-vs-`LISTEN`/`NOTIFY` pickup-latency comparison in `docs/04-benchmarks.md`, plus a test that a dropped notification is still picked up by the poll.

*Concept to internalize:* the polling latency/load tradeoff, `LISTEN`/`NOTIFY` as
an event signal layered over a durable poll, and why the durable path must remain
the source of truth.

---



## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API

- [x] A small typed JSON API: `POST /jobs` to enqueue (validate `queue`/`kind`,
  cap payload size), `GET /jobs/{id}` for status, and a way to **list the
  DLQ** and **requeue** a dead job.
- [x] Sensible status codes (`201` on enqueue, `404` for an unknown id, `400` for
  a malformed body) via the `AppError` → response mapping.
- [ ] Graceful shutdown: stop claiming new work, let in-flight jobs finish (or let
  their leases expire so another worker retries), then exit — never `abort()`
  a worker mid-job and lose the ack.



### State & durability

- [ ] Postgres is the durable source of truth; a job survives a full restart of
  every worker (that's the difference between this and project 03's in-memory
  hub).
- [x] The claim is atomic (V1) and the lease is honoured (V2): assert, with a
  test, that **N concurrent workers never run one job twice concurrently**.
- [ ] A bounded connection pool; the worker count and pool size are tuned
  together (a worker blocked on a connection is a stalled worker).



### Security / abuse protection

- [x] Authenticate the enqueue API (an API key / token) — an open `POST /jobs` is
  an open door to make your workers do arbitrary work.
- [x] Validate and **cap** everything the caller controls: payload size, `queue`/
  `kind` charset and length, `max_attempts` ceiling, `delay` ceiling.
- [ ] Never log payloads blindly (they may carry secrets/PII); never trust `kind`
  to do anything but select a registered handler.



### Observability

- [x] Gauges: queue depth (`ready`), in-flight (`running`), DLQ size, oldest
  ready job's age (**the** lag metric — if this climbs, you're falling behind).
- [x] Counters: jobs enqueued / completed / retried / dead-lettered, leases
  reaped (a non-zero reap rate means workers are dying or leases are too
  short), claims that came back empty.
- [x] Histograms: job execution time and end-to-end latency (enqueue → done) p50/p99. A `tracing` span per job carrying `job.id`, `kind`, and `attempt`.

---



## Cross-cutting scale skills

- Concurrency correctness: a *tested* guarantee that concurrent claims across many
workers (and many processes) hand each job to exactly one worker at a time.
- Failure recovery: a killed worker's in-flight job is provably picked up by
another after the visibility timeout — proven by a test that drops a worker
mid-job.
- Flow control: a defined answer to "the queue is filling faster than workers
drain it" — backpressure on enqueue, or at least the lag metric that tells you.
- Idempotency: an explicit story for the duplicate delivery that at-least-once
guarantees will eventually hand you.



## Definition of done

The project is **done when ALL true:**

1. Every vertical + horizontal box above is checked (each with its **Proof** artifact).
2. A `bench/` load test (a Rust or `k6` client that enqueues a large backlog and
  runs a worker pool to drain it) reporting: sustained **throughput** (jobs/sec)
   and end-to-end **latency** p50/p99 under load; the throughput **with the claim
   index vs. without it** (the V1 payoff); a **chaos run** that kills a worker
   mid-batch and shows every job still completes exactly once *to completion*
   (V2); and a **poll vs. LISTEN/NOTIFY** comparison of pickup latency on an
   otherwise-idle queue (V4). Numbers in `docs/04-benchmarks.md`.
3. A short `docs/04-design.md`: your claim query and the index behind it; the
  lease length you chose and the at-least-once/idempotency reasoning; the retry
   backoff curve and DLQ policy; and the LISTEN/NOTIFY design including why the
   poll fallback stays.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p job-queue` are
  green; no `todo!()` remains on a checked path.



## Suggested order of attack

1. Get the API up: `POST /jobs` inserts a row, `GET /jobs/{id}` reads it back.
  (Add the first real `sqlx` queries here — `docker compose up -d`,
   `sqlx migrate run`, then `cargo sqlx prepare` so compile-time checking works.)
2. Build the claim engine (V1): `FOR UPDATE SKIP LOCKED`, a batch claim, and the
  ack. Run **two** workers against a backlog and prove no job runs twice.
3. Make claims leases and add the reaper (V2): kill a worker mid-job and watch
  another pick the job up after the timeout.
4. Add retry-with-backoff and the DLQ (V3): enqueue a job that always fails and
  watch it back off, then land in the DLQ instead of looping.
5. Add delayed jobs, then swap the worker's poll-sleep for `LISTEN`/`NOTIFY` (V4)
  and measure the pickup-latency drop.
6. Auth the API, add the caps/limits and the queue-depth/lag metrics, then
  benchmark and document.



## Run the dependencies

```bash
docker compose up -d        # postgres
cp .env.example .env        # then fill in values (DATABASE_URL etc.)
sqlx migrate run            # cargo install sqlx-cli --no-default-features -F native-tls,postgres

# Terminal 1 — the API + (optionally) the worker pool:
cargo run -p job-queue
#   RUN_WORKERS=false (default) → enqueue API only; the bare scaffold serves
#   cleanly and a POST /jobs panics with the V1 todo — that panic is the worklist.
#   RUN_WORKERS=true            → spins up the worker pool + reaper too.

# Terminal 2 — enqueue a job:
curl -X POST localhost:8080/jobs \
  -H 'content-type: application/json' \
  -d '{"queue":"default","kind":"send_email","payload":{"to":"a@b.c"}}'

# Multi-process test (V1/V2): run a second `cargo run -p job-queue` with
# RUN_WORKERS=true against the SAME database and watch the two pools share work.
```

## 🔬 From the field

<!-- Adoption backlog distilled from RESEARCH.md by /harvest. NOT graded:
     [~] = open, [✔] = adopted — not counted toward graded progress;
     shown under FROM THE FIELD in status detail.
     Tick a box when the idea has actually landed in this project. -->

### Queue-semantics extras

- [~] Idempotency keys with stored results: enqueueing twice with the same key
  returns the original job instead of creating a second execution — a unique
  constraint in the same transaction as the side effect *(→ RESEARCH.md §Part 2)*
  
- [~] Transactional enqueue (River's headline feature): an in-process caller
  enqueues inside its own DB transaction — roll back and the job never existed;
  the dual-write problem disappears *(→ RESEARCH.md §Part 2)*
- [~] The transactional outbox, end to end: business row + outbox row commit in
  one transaction, a relay publishes at-least-once, and an idempotent consumer
  collapses it to effectively-once *(→ RESEARCH.md §Part 2)*
- [~] Per-key ordering (SQS `MessageGroupId` analog): jobs sharing an ordering
  key run strictly in order while different keys run in parallel — and a stuck
  key blocks only itself *(→ RESEARCH.md §Part 2)*
- [~] Priority without starvation: higher-priority jobs run first, but waiting
  low-priority jobs age upward so sustained high-priority load can never starve
  them forever *(→ RESEARCH.md §Part 2)*
- [~] Lease heartbeat: a long job extends `locked_until` while its worker is
  alive, so the lease length no longer caps job duration — and a stalled worker
  is still reaped *(→ RESEARCH.md §Part 2)*
- [~] Job dependencies / fan-out: a job becomes ready only when all its parent
  jobs complete, so a workflow-shaped graph runs in dependency order
  *(→ RESEARCH.md §Part 1)*
- [~] Per-queue rate limiting (Cloud Tasks model): a queue configured with max
  dispatches/sec and max concurrent never exceeds either, no matter the backlog
  *(→ RESEARCH.md §Part 3)*
- [~] Enqueue backpressure: past a configured depth bound, `POST /jobs` rejects
  (429) instead of letting the backlog grow without bound
  *(→ RESEARCH.md §Part 2)*
- [~] Batch enqueue: N jobs land in one round trip (`COPY`-style), measured
  against N single inserts *(→ RESEARCH.md §Part 3)*
- [~] A retry budget: a system-wide cap on retry volume, so a mass failure
  backs off collectively instead of thundering-herding the recovering
  downstream *(→ RESEARCH.md §Part 2)*

### Postgres-at-scale labs

- [~] Reproduce the queue death spiral: a bench holds a long transaction open
  (pinning xmin) during heavy churn and shows dead tuples accumulating and
  throughput collapsing — with the dead-tuple ratio on the dashboard climbing
  *before* throughput falls *(→ RESEARCH.md §Part 3)*
- [~] TRUNCATE-rotation experiment (Skype PgQ lineage): a rotation-based design
  produces zero dead tuples by construction; its bloat compared against the
  DELETE-based table under the same load *(→ RESEARCH.md §Part 3)*
- [~] UNLOGGED-table experiment: the measured throughput gain of skipping the
  WAL, and a demonstration of the price — the queue table is truncated on crash
  *(→ RESEARCH.md §Part 2 & 3)*
- [~] Autoscaling on the golden signals (KEDA analog): the worker pool grows
  and shrinks driven by queue depth and oldest-ready-job age
  *(→ RESEARCH.md §Part 2)*

### Alternative-substrate labs

- [~] A Redis Streams backend: consumer groups (`XREADGROUP`, the PEL, `XACK`,
  `XAUTOCLAIM`) pass the same acceptance suite as the Postgres backend — same
  semantics, different substrate *(→ RESEARCH.md §Part 3)*
- [~] A hierarchical timing wheel: O(1) insert/expire scheduling for delayed
  jobs (the Kafka-purgatory structure), compared against the timestamp-scan
  approach *(→ RESEARCH.md §Part 2)*

### Correctness practice

- [~] A measured duplicate rate: the chaos bench kills workers in a loop and
  reports duplicate executions per N jobs; the design doc states the threshold
  at which broker-side dedup would become worth building
  *(→ RESEARCH.md §Recommendations 1)*

