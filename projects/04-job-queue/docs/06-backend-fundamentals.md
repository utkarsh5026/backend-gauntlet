# Backend Fundamentals Woven Through the Job Queue

> The cross-cutting skills the SPEC grades *alongside* the four verticals — the API
> surface, security, observability, graceful shutdown, and pool sizing. No prior
> knowledge assumed.
>
> Prepares you for the **Horizontal checklist** in [`SPEC.md`](../SPEC.md) and the
> **Rapid-fire round** in [`CONCEPTS.md`](../CONCEPTS.md). Anchored to
> [`src/routes.rs`](../src/routes.rs), [`src/error.rs`](../src/error.rs),
> [`src/main.rs`](../src/main.rs), [`src/worker.rs`](../src/worker.rs).
> These are woven in as you build the verticals — not bolted on at the end.

---

## The one sentence to hold onto

**A queue is production infrastructure, so the boring parts — who can enqueue, what you
cap, what you can *see*, and how you shut down — are not optional polish; they're what
keeps one open endpoint or one bad deploy from taking you down.**

---

## 1. The producer/admin API — and why enqueue is a dangerous endpoint

The HTTP surface is small and already scaffolded in [`routes.rs`](../src/routes.rs):

| Route | Purpose | Status |
|---|---|---|
| `POST /jobs` | enqueue (validate, cap, then insert) | wired → `enqueue` |
| `GET /jobs/{id}` | inspect one job | wired → `get_job` |
| `GET /healthz` | liveness | wired |
| *list the DLQ* / *requeue a dead job* | admin (V3) | **you add** |

Status codes follow from the [`AppError`](../src/error.rs) mapping already in place:
`201` on enqueue, `404` for an unknown id (`AppError::NotFound`), `400` for a malformed
body (`AppError::BadRequest`), `500` for a DB error. Note enqueue returns the **job
row/id, not a result** — it's async "we accepted this," closer in spirit to `202` than
"here's your answer." The result arrives later, out of band, via a worker.

### Why `POST /jobs` is a security boundary, not a convenience

An open enqueue endpoint is an **"execute arbitrary work on my infrastructure"** door.
Anyone who can POST can make your workers do things and consume your resources.

| Threat | What to do |
|---|---|
| Anyone enqueuing work | **Authenticate** enqueue (API key / token) |
| A giant payload OOMs a worker | **Cap** payload size |
| `queue`/`kind` used as an injection or resource-exhaustion vector | validate charset + length; treat `kind` as *only* a lookup into a **registered** handler table — never eval it |
| `max_attempts: 1000000`, `delay_secs: 10^9` | ceiling every caller-controlled number |
| Secrets/PII in `payload` leaking to logs | never log payloads blindly |

The scaffold flags exactly this in the `enqueue` handler's `TODO(security)` comment —
authenticate and validate *before* touching the queue.

---

## 2. Observability — the one metric that tells you you're losing

You can't operate a queue you can't see. The SPEC asks for three families of signal:

**Gauges (current state):**
- queue depth (`ready` count), in-flight (`running` count), DLQ size
- **oldest ready job's age** — *the* lag metric

**Counters (rates):**
- jobs enqueued / completed / retried / dead-lettered
- **leases reaped** — a non-zero rate means workers are dying *or* leases are too short
- claims that came back empty

**Histograms (distributions):**
- job execution time, end-to-end latency (enqueue → done) p50/p99
- a `tracing` span per job carrying `job.id`, `kind`, `attempt`

### Why "oldest ready age" beats "queue depth"

This is the subtle one. Queue *depth* alone **can lie**:

```
Scenario A: depth = 500, steady.  Workers drain 500/s, arrivals 500/s.
            Oldest ready job is 1s old. → healthy equilibrium.

Scenario B: depth = 500, steady.  Workers drain 100/s, arrivals 100/s,
            but 400 jobs have been stuck ready for 10 minutes.
            Oldest ready job is 600s old. → you are FALLING BEHIND.
```

Same depth, opposite health. **Age of the oldest ready job** is the honest lag signal:
if it climbs, work is arriving faster than you drain it, and no amount of steady depth
hides that. Wire this metric and alert on *it*.

---

## 3. Graceful shutdown — never abort a job mid-run

A deploy or a `SIGTERM` will hit a worker that's mid-job. The wrong move is to
`abort()` the task — you'd lose the ack for work that already happened (or half-did),
turning a routine restart into duplicate or lost effects. The right order:

```
1. stop CLAIMING new work        (don't start what you can't finish)
2. let in-flight jobs finish     (ack them) — OR just let their leases lapse,
                                   and another worker retries them (at-least-once!)
3. then exit
```

The wiring is already there: [`main.rs`](../src/main.rs) broadcasts shutdown over a
`watch` channel, and [`worker::run`](../src/worker.rs) checks it between jobs. The
`shutdown_signal` function carries a `TODO(SPEC)` to get this order right. Notice the
lease (V2) makes shutdown *safe by default*: even a hard kill mid-job just leaves an
expired lease the reaper cleans up — you never *need* to abort to avoid losing a job.

---

## 4. Connection pool vs. worker count — tune them together

Two numbers from [`.env.example`](../.env.example): `DB_MAX_CONNECTIONS` (the Postgres
pool, default 20) and `WORKER_CONCURRENCY` (workers, default 4). They're coupled:

> **A worker blocked waiting for a DB connection is a stalled worker.**

Every worker needs a connection to claim, ack, and nack. If you run 50 workers against
a pool of 20, up to 30 workers can be parked waiting for a connection at any moment —
you paid for 50 workers and got 20 workers' worth of throughput, plus contention. And a
LISTEN connection (V4) is typically held *separately*, so budget for it. Rough starting
point: pool ≥ workers (+ headroom for the reaper, listeners, and the HTTP handlers).
The right ratio is something you *measure* in the benchmark, not guess.

---

## 5. Flow control — what happens when you can't keep up

At-least-once + retries means load can *grow* under stress (failures re-enqueue
themselves). You need a defined answer to "arrivals outpace drain":

- **Backpressure on enqueue** — reject/slow producers when depth or lag crosses a
  threshold (a `429`/`503`), or
- **At minimum, the lag metric** (§2) so you *know* it's happening and can add workers.

Deciding which — and where the threshold sits — is a design call to record in
[`04-design.md`](./04-design.md).

---

## Depth probes (you own the fundamentals when you can answer)

- Why can a steady queue depth still mean you're falling behind — and which single
  metric exposes it?
- Why is aborting a worker mid-job *never necessary* in this design? (What safety net
  makes a hard kill acceptable?)
- Why does an open `POST /jobs` deserve auth even inside a "trusted" network?
- Why can't you size the worker pool without also sizing the DB connection pool?

---

## Where you'll build this

| Piece | Location |
|---|---|
| auth + input caps on enqueue | [`enqueue`](../src/routes.rs) `TODO(security)` |
| DLQ list + requeue endpoints | add to [`routes.rs`](../src/routes.rs) (V3) |
| error → status mapping | [`AppError`](../src/error.rs) (extend as needed) |
| graceful-shutdown order | [`shutdown_signal`](../src/main.rs) `TODO(SPEC)` + [`worker::run`](../src/worker.rs) |
| metrics (gauges/counters/histograms) | wire across `queue.rs`, `lease.rs`, `worker.rs` |
| pool/worker tuning | `DB_MAX_CONNECTIONS` / `WORKER_CONCURRENCY` in [`main.rs`](../src/main.rs) |

**This doc unlocks (Horizontal checklist):** the typed JSON API with DLQ list/requeue
and sensible status codes; graceful shutdown; authenticated + capped enqueue; the
queue-depth / in-flight / DLQ / oldest-ready-age gauges, the enqueue/complete/retry/
dead/reaped counters, and the execution-time / end-to-end-latency histograms.

**Ready to build?** These weave into every vertical — do the security caps when you
build enqueue (V1), the reaped-leases metric with the reaper (V2), the DLQ endpoints
with retries (V3). `/hint 04` if you get stuck on any piece.
</content>
