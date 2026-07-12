# Concept Bank — Project 04: Distributed Job Queue

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The atomic claim: `FOR UPDATE SKIP LOCKED` *(V1 · `src/queue.rs`)*

**The problem.** Two workers both run `SELECT id FROM jobs WHERE state='ready' LIMIT 1`, both get row 42, both `UPDATE` it and both run the job. Sending the same email twice, charging the same card twice. The read and the write were individually correct; the *gap between them* is the bug. This is the database flavor of the same TOCTOU race from project 02.

**The idea.** Fuse the select and the claim into one atomic step. `FOR UPDATE` row-locks the candidate rows inside the transaction; `SKIP LOCKED` makes the second worker *step over* rows the first already locked instead of blocking behind them. The result is contention-free hand-off: N workers can dip into one table simultaneously and each gets different jobs, at full speed. Claim in batches (`LIMIT n`) so a worker isn't paying a DB round-trip per job, and back the ready-scan with a partial index so it stays cheap at millions of rows.

**In the wild:** this exact pattern powers Sidekiq-on-Postgres clones, Oban (Elixir), Solid Queue (Rails 8), Graphile Worker, good_job — "Postgres as a queue" became mainstream because SKIP LOCKED made it correct.

**You own it when you can explain:**
- [ ] The double-dispatch interleaving, step by step, and why application-level care can't fix a between-processes race.
- [ ] What `FOR UPDATE` locks and for how long; what `SKIP LOCKED` changes about the second worker's behavior (skip vs block) and why blocking would serialize the pool.
- [ ] Why batch claiming matters — where a claim-one-at-a-time worker actually spends its time.
- [ ] What a *partial* index (`WHERE state='ready'`) buys over a full index on a table that's 99% done jobs.
- [ ] Why the claim respects `run_at <= now()` and oldest-first — scheduling and fairness ride on the same query.

**Depth probes:**
- Why does this pattern require the lock to live *inside* a transaction? What happens to the locks if the worker's connection dies mid-claim?
- At what scale does "Postgres as a queue" stop making sense, and what specifically breaks first?

**Trap:** adding `SKIP LOCKED` without understanding the lock lifetime. If you claim inside a transaction and hold it open for the whole job, long jobs pin connections and vacuum; that's why the claim *stamps state and commits*, and the lease (Card 2) takes over.

---

## 🧠 Card 2 — Leases, at-least-once, and the idempotency bill *(V2 · `src/lease.rs`)*

**The problem.** A worker claims a job and gets OOM-killed halfway. The row says `running`. No worker will touch it; it isn't done; it isn't retryable. It's stuck — forever, silently. Any distributed queue where a claim is permanent ownership loses jobs to every crash.

**The idea.** A claim is a **lease**: "you may try until `locked_until`". Success acks the job to `done`; a crash simply lets the lease expire, and a reaper sweeps expired `running` jobs back to `ready` for someone else. This buys *at-least-once* execution — and you must pay its bill openly: a worker can finish the work and die *before acking*, so the job runs again. There is no exactly-once execution in a distributed system; there is at-least-once delivery plus **idempotent handlers**, which together *simulate* exactly-once effects.

**In the wild:** SQS visibility timeout (the name comes from there), Google Cloud Tasks, Temporal activity timeouts (project 21), Kafka consumer session timeouts — the same triangle everywhere.

**You own it when you can explain:**
- [ ] The lease lifecycle: claim → stamp `locked_by/locked_until` → work → ack; and the reaper's role when the ack never comes.
- [ ] At-least-once vs at-most-once as a *choice of when you ack* (after vs before the work) — and which failure each choice accepts.
- [ ] Why exactly-once *delivery* is impossible (the ack itself can be lost) and why idempotent *effects* are the honest workaround.
- [ ] The lease-length dial: too short reruns slow-but-alive jobs; too long delays crash recovery — and how a heartbeat ("still working, extend me") serves long jobs.
- [ ] Two ways to make a handler idempotent: natural idempotency (UPSERT, set-to-value) vs an idempotency-key dedup table.

**Depth probes:**
- The reaper reclaims a job whose worker is actually still alive, just slow. Now two workers run it concurrently. Which of your guarantees survives, and what makes that safe?
- Why does a non-zero "leases reaped" metric deserve an alert threshold rather than zero-tolerance?

**Trap:** believing a framework someday gives you exactly-once so you can skip idempotency. Every "exactly-once" product feature (Kafka EOS included) is at-least-once plus dedup underneath — the handler discipline never becomes optional.

---

## 🧠 Card 3 — Retries, backoff + jitter, and the DLQ *(V3 · `src/retry.rs`)*

**The problem.** Jobs fail. Retry immediately, forever, and one job whose payload always crashes the handler — a **poison message** — occupies your workers in an infinite hot loop. Retry on a fixed interval and every job that failed together (say, when a downstream API blipped) retries *together*, re-creating the spike that broke things — a self-inflicted thundering herd. Even exponential backoff without jitter synchronizes: everyone comes back at exactly t+2, t+4, t+8.

**The idea.** Retry with exponentially growing, jittered, capped delays — the exponential spreads load over time, the jitter spreads it across time, the cap keeps recovery latency sane. And retries must *end*: after `max_attempts`, the job moves to a dead-letter queue — a terminal parking lot that keeps one broken job from becoming an outage, while staying inspectable and requeueable so it's not silent data loss.

**In the wild:** AWS's "Exponential Backoff and Jitter" post is the canonical reference; every SQS/RabbitMQ deployment has a DLQ; Stripe's webhook retries (which you'll build in project 18) follow the same curve.

**You own it when you can explain:**
- [ ] Why immediate retries turn one poison message into 100% worker occupancy.
- [ ] The herd-synchronization argument: why fixed intervals *and* bare `2^n` both cluster retries, and what jitter actually decorrelates.
- [ ] The full policy as a formula: `delay = min(cap, base × 2^attempt) × random_factor` — and what removing each term breaks.
- [ ] What qualifies a job for the DLQ vs another retry (attempts exhausted; optionally: error classified as permanent).
- [ ] Why DLQ inspect + requeue is a hard requirement — the "dead job you can't see" failure story.

**Depth probes:**
- Which errors should skip retries entirely and go straight to the DLQ? (Validation errors, 4xx-class failures — retrying can't fix them.)
- Retried jobs may execute *out of order* relative to newer jobs. When does that matter, and what would ordered retries cost?

**Trap:** retrying without classifying the error. Retrying a permanent failure (malformed payload) wastes the whole backoff budget to arrive at the DLQ anyway — with hours of added latency for a job that was never going to work.

---

## 🧠 Card 4 — Wakeups without busy-polling: LISTEN/NOTIFY *(V4 · `src/scheduler.rs`)*

**The problem.** A worker discovers new jobs by polling. Poll every 50 ms and an idle system hammers Postgres with empty SELECTs all night. Poll every 5 s and every job eats up to 5 s of pointless latency. The knob only *moves* the pain between load and latency — no setting removes it.

**The idea.** Layer an event signal over the durable poll: enqueue fires `NOTIFY` on the queue's channel; idle workers `LISTEN` and wake instantly. Result: millisecond pickup *and* a quiet idle database. The crucial design rule: `NOTIFY` is fire-and-forget — not durable, lost on disconnect, invisible to a worker that was mid-restart — so a slow fallback poll remains, and the *poll* stays the source of truth. The notify is an optimization; correctness never depends on it.

**In the wild:** Graphile Worker and Oban use exactly this; the general shape — durable state + best-effort wake signal — is everywhere (SQS long-polling, Redis blocking pops, condition variables over a locked queue).

**You own it when you can explain:**
- [ ] The latency-vs-load tradeoff of pure polling, and why tuning the interval can't eliminate it.
- [ ] The mechanics: who NOTIFYs (enqueue, retry-due, delay-due), who LISTENs, and what a woken worker actually does (runs the normal claim).
- [ ] Why the fallback poll is non-negotiable: three concrete ways a NOTIFY gets lost.
- [ ] The design principle worth generalizing: *durable pull for truth, ephemeral push for latency*.
- [ ] How delayed jobs (`run_at` in the future) interact with this — who wakes the worker when the delay expires?

**Depth probes:**
- 100 idle workers all LISTEN; one job arrives; all 100 wake and race the claim. Is that a problem? What bounds the damage (SKIP LOCKED again)?
- Compare with Redis `BLPOP` and SQS long-polling — what does each runtime give or lose?

**Trap:** making NOTIFY carry the job payload. The moment the notification is load-bearing data, its non-durability becomes data loss; it should carry at most "check the queue".

---

## ⚡ Rapid-fire round

- [ ] The one falling-behind metric: oldest ready job's age — why queue *depth* alone can lie (steady depth can hide steady lag).
- [ ] What a rising "leases reaped" count means operationally (dying workers? too-short lease?).
- [ ] Graceful shutdown order: stop claiming → finish in-flight (or let leases lapse) → exit — and why aborting mid-job is never needed.
- [ ] Why worker count and DB pool size are tuned together (a worker blocked on a connection is a stalled worker).
- [ ] Why an open enqueue API is an "execute work on my infrastructure" endpoint, and what you cap (payload size, max_attempts, delay ceiling).
- [ ] Why `202`-style async semantics fit enqueue (`201` returns the job row, not the result).

## 🔗 Connects to

- The lease/reaper pattern is reused *verbatim* in project 12 (transcode tasks) and project 21 (workflow task dispatch) — this project is where you earn it.
- Backoff + jitter + DLQ returns in project 18's webhook delivery.
- `SKIP LOCKED` claiming is project 21's timer scanner and project 16's transcode queue.
