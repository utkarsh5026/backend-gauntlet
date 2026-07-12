# How a Distributed Job Queue Works — From First Principles

> A beginner-friendly guide. No prior backend knowledge assumed.
> It explains the *ideas* — and how *this project's actual code* is shaped around
> them — before you fill in a single `todo!()`.
>
> Anchored to the real scaffold: [`migrations/0001_init.sql`](../migrations/0001_init.sql),
> [`src/job.rs`](../src/job.rs), [`src/queue.rs`](../src/queue.rs),
> [`src/lease.rs`](../src/lease.rs), [`src/retry.rs`](../src/retry.rs),
> [`src/scheduler.rs`](../src/scheduler.rs), [`src/worker.rs`](../src/worker.rs),
> [`src/routes.rs`](../src/routes.rs). Read this first, then [`SPEC.md`](../SPEC.md).
>
> This teaches the *concepts* and how the existing wiring fits together. It does
> **not** hand you the `todo!()` bodies — those are the point of the project.

---

## 1. The one sentence to hold onto

**A job queue is a database table that you treat as a to-do list: producers `INSERT`
rows ("please do this later"), workers atomically claim rows, run them, and mark
them done — and every hard part is about what happens when there are many workers
and things crash.**

Everything below is a consequence of that one sentence.

---

## 2. What even *is* a job queue? A concrete scenario

You run a website. A user signs up. You want to send them a welcome email.

The **naive** way — send the email *right there* inside the signup request:

```
POST /signup
  ├── write the user to the database
  ├── call the email provider's API  ← takes 800ms, sometimes fails
  └── return 200 to the browser
```

This is bad, and every reason it's bad is a reason job queues exist:

| Problem with "do it inline" | What the user experiences |
|---|---|
| The email API is slow (800ms) | Signup *feels* slow — they wait on work they don't care about |
| The email API is **down** right now | Signup **fails**, even though the account is fine |
| The email API rate-limits you | Some signups randomly fail under load |
| You want to *retry* a failed send | You'd have to block the request even longer |
| You add "send a Slack ping" too | Now signup waits on *two* flaky services |

The fix is to **decouple** "something needs to happen" from "it happened":

```
POST /signup
  ├── write the user to the database
  ├── enqueue a job: {kind: "send_welcome_email", payload: {user_id: 42}}  ← ~1ms
  └── return 200 immediately

... meanwhile, out of band ...

worker: pick up the job → call the email API → mark it done
        (if it fails: wait a bit, try again; if it keeps failing: set it aside)
```

The signup is fast and reliable again. The slow, flaky work moved **behind the
queue**, where it can be retried, rate-limited, and observed without a human waiting.

That "enqueue a job" / "worker picks it up" split is the entire product. This project
is: **build that, correctly, on top of Postgres.**

> **Why Postgres and not RabbitMQ / SQS / Sidekiq?** Those are real queue brokers you'd
> normally reach for. The whole point here is to *not* reach for one — to discover that
> a plain SQL table plus four ideas (atomic claim, lease, retry policy, wakeup signal)
> **is** a queue broker. Postgres does double duty: it's both the **durable store** and
> the **broker**. See the note at the top of [`migrations/0001_init.sql`](../migrations/0001_init.sql).

---

## 3. The producer / consumer / table model

Three parts, and it helps to hold them apart in your head:

```
   PRODUCER                      THE TABLE (jobs)                    CONSUMER(S)
 (whoever enqueues)            durable, shared, ordered          (the worker pool)

  POST /jobs  ──INSERT──▶   ┌──────────────────────────┐   ◀──claim──  worker-0
                            │ id | kind | state | ...   │   ◀──claim──  worker-1
                            │ 42 | email| ready | ...   │   ◀──claim──  worker-2
                            │ 43 | email| ready | ...   │   ◀──claim──  worker-3
                            └──────────────────────────┘
```

- **Producer** — anything that calls `POST /jobs`. In this repo that's the HTTP API in
  [`src/routes.rs`](../src/routes.rs) → [`Queue::enqueue`](../src/queue.rs). It just
  writes a row and returns.
- **The table** — one Postgres table, `jobs`. It is the *only* shared state. Because
  it's Postgres, it's **durable**: if every worker process dies and restarts, the jobs
  are still there. (That durability is the headline difference between this project and
  project 03's in-memory pub/sub hub.)
- **Consumers** — a pool of **workers**. In this project they're async tasks spawned in
  [`main.rs`](../src/main.rs) (`WORKER_CONCURRENCY` of them), each running the loop in
  [`src/worker.rs`](../src/worker.rs). Crucially, you can also run *multiple processes*
  of `job-queue` against the **same** database — that's "distributed."

The producer and consumers never talk to each other directly. They only ever talk to
the table. That indirection is what buys you durability and decoupling — and it's also
the source of every concurrency headache below.

---

## 4. The data model — the real `jobs` table

Open [`migrations/0001_init.sql`](../migrations/0001_init.sql). One table backs the whole
system. Here's every column and *why it exists* (which vertical needs it):

| Column | Type | What it's for | Vertical |
|---|---|---|---|
| `id` | `BIGSERIAL` | Unique job identity; returned by enqueue, used by `GET /jobs/{id}` | — |
| `queue` | `TEXT` | Named lane (`"default"`, `"emails"`, `"exports"`). A worker drains one lane. | V1 |
| `kind` | `TEXT` | *Which handler runs this* — e.g. `"send_email"`. The worker dispatches on it. | — |
| `payload` | `JSONB` | Opaque arguments for the handler (`{"to": "a@b.c"}`) | — |
| `state` | `TEXT` | The lifecycle: `ready` / `running` / `done` / `dead` | all |
| `attempts` | `INT` | How many times this job has been tried | V3 |
| `max_attempts` | `INT` | The ceiling before it's given up on (default 5) | V3 |
| `last_error` | `TEXT` | Why the most recent attempt failed | V3 |
| `run_at` | `TIMESTAMPTZ` | **Not claimable until `run_at <= now()`** — powers delays *and* retry backoff | V1/V3/V4 |
| `locked_at` / `locked_until` / `locked_by` | `TIMESTAMPTZ` / `TEXT` | The **lease**: who holds this job and until when | V2 |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | bookkeeping | — |

The same shape shows up in Rust as [`struct Job`](../src/job.rs) (what `claim` hands a
worker) and [`struct NewJob`](../src/job.rs) (what a producer supplies to `enqueue`).

Two columns deserve special attention because they carry more weight than they look:

- **`run_at` is the scheduling primitive.** "Run this in 5 minutes," "retry this in 8
  seconds after backoff," and "run this now" are *all* just different values of `run_at`.
  A job is invisible to the claim until its `run_at` has passed. One column, three
  features. See `delay_secs` in [`NewJob`](../src/job.rs).
- **`state` is a state machine, not a label.** The whole project is about which
  transitions are legal and how they happen atomically.

---

## 5. The lifecycle: a state machine

[`JobState`](../src/job.rs) has exactly four states. Here's how a job moves through them:

```
                         enqueue (INSERT)
                                │
                                ▼
        ┌───────────────────▶ READY ◀────────────────────┐
        │  reaper: lease         │                        │
        │  expired (V2)          │ claim (V1)             │ nack: attempts
        │                        │ FOR UPDATE SKIP LOCKED │ remain → backoff,
        │                        ▼                        │ run_at = now()+delay (V3)
        │                     RUNNING ─────────────────────┘
        │                        │
        │   worker acks (V1)     │   worker fails the job
        ▼                        ▼
     (crash: no ack,          success            nack: attempts
      lease lapses)              │                exhausted (V3)
                                 ▼                        │
                               DONE                       ▼
                             (terminal)                 DEAD
                                                    (terminal, the DLQ)
```

Read the two terminal states as the two ways a job's life ends:

- **`done`** — it succeeded and was **acked**. Never touched again.
- **`dead`** — it failed `max_attempts` times and landed in the **dead-letter queue**.
  Set aside so it stops wasting workers, but kept around so you can inspect and requeue it.

Every arrow in that diagram is one of the four verticals. Now let's earn each one, starting
with the bug that makes the whole thing hard.

---

## 6. The problem before the solution: the double-dispatch race

Here's the naive claim. It looks obviously correct:

```sql
-- Worker wants a job. Step 1: find one.
SELECT id FROM jobs WHERE state = 'ready' LIMIT 1;   -- returns id 42
-- Step 2: take it.
UPDATE jobs SET state = 'running' WHERE id = 42;
```

Run **one** worker: perfect. Run **two** workers at the same time and trace the
interleaving carefully:

| Time | worker-0 | worker-1 | Table |
|---|---|---|---|
| t1 | `SELECT ... LIMIT 1` → **42** | | job 42 is `ready` |
| t2 | | `SELECT ... LIMIT 1` → **42** | job 42 is *still* `ready` |
| t3 | `UPDATE 42 → running` | | job 42 is `running` |
| t4 | | `UPDATE 42 → running` | job 42 is `running` (again — no-op) |
| t5 | runs job 42 (sends the email) | runs job 42 (**sends the email AGAIN**) | 💥 |

Both workers read `42` in the gap **before** either wrote. The `SELECT` was correct.
The `UPDATE` was correct. The *gap between them* is the bug. The welcome email goes out
twice; a payment gets charged twice.

This is a **TOCTOU** race — Time-Of-Check To Time-Of-Use — the exact same shape of bug
you met in project 02's rate limiter, now wearing a database costume. And note **you
cannot fix it in application code**: the two workers might be in *different processes on
different machines*. There is no mutex you both share. The only thing you both share is
Postgres. So the fix has to live *in the query*.

| "Just add a check" idea | Why it still races |
|---|---|
| `SELECT`, then `UPDATE ... WHERE state='ready'` | Both still `SELECT` 42; the second `UPDATE` matches 0 rows but the worker already *decided* to run 42 |
| An app-level lock / mutex | Doesn't span processes or machines; useless once you scale out |
| `SELECT ... FOR UPDATE` (lock but no skip) | Correct, but worker-1 **blocks** waiting for worker-0's lock, then gets the *same row* — the pool serializes and still fights over 42 |

The last row is the key insight: locking alone isn't enough. You need the second worker
to **give up on the locked row and move to a different one**. That's V1.

---

## 7. V1 — the atomic claim: `FOR UPDATE SKIP LOCKED`

*(concept for [`Queue::claim`](../src/queue.rs); the SQL shape is sketched in the
`todo!()` there and in [the migration](../migrations/0001_init.sql) — your job is to
implement and wire it.)*

The fix is to **select and claim in one atomic statement**, with two magic modifiers:

- **`FOR UPDATE`** — "lock the rows I'm selecting, inside this transaction, so no one else
  can claim them until I'm done."
- **`SKIP LOCKED`** — "and if a row I'd have selected is *already* locked by someone else,
  don't wait for it — **skip it** and take the next available one instead."

Together they turn the table into a contention-free hand-off. Trace the same two workers
again, now with `SKIP LOCKED`:

| Time | worker-0 | worker-1 | Result |
|---|---|---|---|
| t1 | claims → locks **42**, skips nothing else | claims → 42 is locked, **skips it**, locks **43** | different rows! |
| t2 | gets job 42 | gets job 43 | ✅ no overlap |

N workers can dip into the same table simultaneously and each walks away with a
*different* batch, at full speed, no blocking. That's the whole trick that made
"Postgres as a queue" mainstream (Sidekiq-on-PG, Oban, Solid Queue, Graphile Worker,
good_job all do exactly this).

Three details the SPEC makes you get right:

1. **Claim a *batch*, not one row.** A round-trip to Postgres costs ~1ms. If a worker
   claims one job, runs it, and round-trips again, it spends most of its life waiting on
   network latency instead of doing work. Claim `LIMIT n` (this project: `CLAIM_BATCH`,
   default 10) so one round-trip feeds many jobs. See `claim_batch` in
   [`WorkerConfig`](../src/worker.rs).

2. **Order and filter correctly.** The claim must respect `run_at <= now()` (don't grab
   jobs scheduled for the future), `queue = $1` (only this lane), and `ORDER BY run_at`
   (oldest first — fairness).

3. **Index the hot path.** As the table grows to millions of rows — 99% of them `done` —
   scanning for `ready` rows gets slow. A **partial index** (`... WHERE state = 'ready'`)
   indexes *only* the tiny slice of live rows, so the claim stays fast no matter how much
   history piles up. The migration deliberately leaves this out — adding it and proving
   the difference with `EXPLAIN`/a benchmark is a V1 lesson.

> **Why must the lock live inside a transaction?** `FOR UPDATE` locks are released when
> the transaction ends. If you held the transaction open for the *whole job*, a slow job
> would pin a database connection and block Postgres's vacuum for minutes. So the claim
> **stamps the row's state and commits immediately** — the transaction is short. But then
> what stops *another* worker from grabbing the now-committed `running` row, or reclaiming
> it if this worker crashes? That's exactly the gap the **lease** fills. On to V2.

---

## 8. V2 — the lease (visibility timeout) and at-least-once

*(concept for [`lease.rs`](../src/lease.rs) and the lease stamp inside
[`Queue::claim`](../src/queue.rs).)*

A claim is **not** "this job is done." It's **"this worker is *allowed to try* for a
while."** That "for a while" is a **lease** (Amazon SQS calls it a *visibility timeout*).

When a worker claims a job, it doesn't just flip `state='running'` — it also stamps:

- `locked_by` = which worker (e.g. `"worker-2"`)
- `locked_until` = `now() + visibility_timeout` (this project: `VISIBILITY_TIMEOUT_SECS`, default 30s)

While `state='running'`, no other worker can see the job (the claim only looks at
`ready` rows). Two things can happen:

```
   worker claims job 42, lease until 12:00:30
        │
        ├── SUCCESS by 12:00:12 → worker ACKs → state='done'          ✅ normal
        │
        └── worker gets OOM-killed at 12:00:08, before acking
                 │
                 job 42 sits: state='running', locked_until=12:00:30
                 │
                 (nobody can claim it — it's not ready, not done, not dead)
                 │
             ... 12:00:30 passes, the lease has EXPIRED ...
                 │
             the REAPER sweeps it: state='ready', clear the lock  →  another worker retries
```

That reaper is the entire reason a crashed worker doesn't lose its job. In this project
it's [`reap_expired`](../src/lease.rs), run on a timer by the already-wired
[`reap_loop`](../src/lease.rs) (every `REAPER_INTERVAL_SECS`). Conceptually it's:
"any `running` job whose `locked_until` is in the past → back to `ready`."

### The bill you must pay: at-least-once, and idempotency

This design buys you **at-least-once delivery** — every job runs *at least* once, even
across crashes. But stare hard at one interleaving:

```
worker finishes the real work (email sent!) at 12:00:29 ...
   ... then crashes at 12:00:29.5, BEFORE the ack commits ...
      ... lease expires at 12:00:30, reaper requeues it ...
         ... another worker runs it → email sent AGAIN.
```

There is **no free exactly-once**. The ack itself can be lost, so duplicates are
*always* possible in a distributed system. The honest, industry-standard answer is not
"prevent duplicates" — it's **make the handler idempotent**: running it twice has the
same effect as running it once. Two common ways:

- **Natural idempotency** — the operation is already safe to repeat: `UPDATE users SET
  verified=true`, an UPSERT, "set balance to X." Running it twice changes nothing.
- **An idempotency key** — record "I already did job 42" in a dedup table; on a repeat,
  see the key and skip.

Every "exactly-once" product feature you'll ever meet (Kafka EOS included) is
at-least-once + dedup underneath. The handler discipline never becomes optional. That's
why in this project the actual work lives in [`handle`](../src/worker.rs) — *your*
handlers — and making them idempotent is *your* responsibility.

### The lease-length dial

`visibility_timeout` is a genuine tradeoff, not a value to guess:

| Lease too **short** | Lease too **long** |
|---|---|
| A slow-but-alive job gets reaped out from under a working worker → runs twice for no reason | A genuinely crashed worker's job sits stuck for a long time before anyone retries it → slow recovery |

The stretch fix for long jobs is a **heartbeat**: a long handler periodically calls
[`extend_lease`](../src/lease.rs) ("still working, push my deadline out") so it's never
reaped while alive.

---

## 9. V3 — retries, backoff + jitter, and the dead-letter queue

*(concept for [`retry.rs`](../src/retry.rs) — [`RetryPolicy::backoff`](../src/retry.rs)
and [`nack`](../src/retry.rs).)*

Jobs fail. The email provider blips; a payload is malformed. The worker's failure path is
already wired in [`process_one`](../src/worker.rs): on `Err`, it calls
[`nack`](../src/retry.rs). The *policy* inside `nack` is what keeps one bad job from
sinking the system. Two failure modes to design against:

**Failure mode 1 — the poison message.** A job whose payload *always* crashes the handler.
Retry it immediately, forever, and it pins a worker in a 100%-CPU hot loop, re-failing
thousands of times a second. The release valve is the **dead-letter queue**: after
`max_attempts`, stop retrying and move the job to `state='dead'`. It's set aside, not
lost — you can inspect *why* it kept failing and requeue it after a fix. A dead job you
*can't* see is silent data loss, so the DLQ must be inspectable and requeueable.

**Failure mode 2 — the retry stampede.** Say a downstream API goes down and 10,000 jobs
all fail in the same second. If they all retry on a fixed 5-second timer, then 5 seconds
later all 10,000 hit the recovering API *simultaneously* — you re-create the exact spike
that broke it. A self-inflicted **thundering herd**.

The fix is **exponential backoff with jitter, capped**:

```
delay = min(  cap,  base × 2^(attempt-1)  )  ×  random_jitter
        └─cap─┘     └──── exponential ────┘     └── decorrelate ──┘
```

Each term earns its place — remove any one and something breaks:

| Term | What it does | Remove it and… |
|---|---|---|
| `base × 2^(attempt-1)` | Grows the wait: 1s, 2s, 4s, 8s… so a struggling downstream gets breathing room | Retries hammer a service that's already down |
| `min(cap, …)` | Ceiling (this project: `max_delay`, default 300s) so `2^n` doesn't schedule a job *absurdly* far out | Uncapped, a job that failed 20 times would wait `2^19` s ≈ **6 days** |
| `× random_jitter` | Spreads the herd across time | Even exponential clusters: everyone retries at *exactly* t+2, t+4, t+8 — still a spike |

That last row is the subtle one: exponential backoff *without* jitter still
synchronizes, because all the jobs that failed together share the same `2^n` schedule.
Jitter is what actually decorrelates them. (AWS's "Exponential Backoff and Jitter" post
is the canonical reference; you'll build this same curve again for webhook delivery in
project 18.)

The scaffold's [`RetryPolicy`](../src/retry.rs) already holds `base_delay` and
`max_delay`; the `backoff` function is yours to write (and there's a `proptest` dev-dep
waiting to property-test that it's monotonic, capped, and actually jittered).

---

## 10. V4 — waking workers without busy-polling: `LISTEN` / `NOTIFY`

*(concept for [`scheduler.rs`](../src/scheduler.rs) — [`wait_for_work`](../src/scheduler.rs)
and [`notify_ready`](../src/scheduler.rs).)*

By now the queue *works*. But look at what an **idle** worker does in
[`worker.rs`](../src/worker.rs): when a claim comes back empty, it
`sleep(poll_interval)` and tries again. That fixed sleep is a real dilemma:

```
poll every 50ms  → an idle system fires ~20 empty SELECTs/sec/worker all night → wasted DB load
poll every 5s    → a job enqueued at 12:00:00.01 isn't picked up until 12:00:05 → up to 5s of latency
```

Tuning the interval only **moves** the pain between *load* and *latency* — no single value
removes it. That's the tradeoff V4 dissolves.

The fix layers a **push signal over the durable pull**:

- When a producer enqueues a job (or a delayed/retried job becomes due), it fires
  Postgres **`NOTIFY`** on the queue's channel — a tiny "hey, there's work" ping.
  ([`notify_ready`](../src/scheduler.rs); the SPEC also suggests emitting it right from
  `enqueue`.)
- An idle worker, instead of sleeping blindly, holds a **`LISTEN`** connection and waits
  on it. The instant a `NOTIFY` arrives, it wakes and claims — **millisecond** pickup —
  and an idle queue makes **zero** empty queries. ([`wait_for_work`](../src/scheduler.rs),
  built on sqlx's `PgListener`.)

The result is the best of both: instant pickup *and* a silent idle database.

### The rule that makes this safe

`NOTIFY` is **fire-and-forget**. It is *not* durable: it's lost if no one is listening at
that instant, lost across a worker reconnect, and it can't wake a worker for a job whose
`run_at` is still in the future. So:

> **The durable poll stays the source of truth; `NOTIFY` is only an optimization.**

That's why [`wait_for_work`](../src/scheduler.rs) waits on the notification **or** a
`poll_fallback` timeout, whichever comes first — and re-attempts a claim regardless.
Miss a notification? The slow poll still catches the job a moment later. If you ever made
correctness *depend* on the notification arriving, a single dropped ping would strand a
job forever. Keep the notify as a latency optimization layered over a system that's already
correct without it. (This "durable pull for truth, ephemeral push for latency" shape is
everywhere: SQS long-polling, Redis `BLPOP`, condition variables over a locked queue.)

> **Won't 100 idle workers all wake on one `NOTIFY` and stampede the claim?** Yes, they
> all wake — and it's fine, because the claim is `FOR UPDATE SKIP LOCKED` (V1). One wins
> each row, the rest skip to other rows or come back empty. V1 is what bounds the damage.

---

## 11. End-to-end trace: one job, cradle to grave

Follow a single job all the way through the *real* call path.

**Enqueue** (producer side, [`routes.rs`](../src/routes.rs) → [`queue.rs`](../src/queue.rs)):

```
POST /jobs  {"queue":"emails","kind":"send_welcome","payload":{"user_id":42}}
   │
   ├─ routes::enqueue extracts NewJob                       (src/routes.rs)
   ├─ queue.enqueue(new)  →  INSERT ... RETURNING id  (V1)  (src/queue.rs)
   │     row: state='ready', run_at=now(), attempts=0, max_attempts=5
   ├─ (V4) NOTIFY jobs_emails  — wake any idle worker
   └─ 201 Created  {"id": 91}     ← producer is done in ~1ms
```

**Drain** (consumer side, [`worker.rs`](../src/worker.rs)):

```
worker-2's loop:
   ├─ (V4) wait_for_work("emails") wakes on the NOTIFY          (src/scheduler.rs)
   ├─ (V1) queue.claim("emails","worker-2", batch=10, 30s)      (src/queue.rs)
   │        FOR UPDATE SKIP LOCKED → job 91 flips to 'running',
   │        locked_by='worker-2', locked_until=now()+30s
   ├─ process_one(job 91):                                      (src/worker.rs)
   │     └─ handle(job)  → YOUR send_welcome handler runs       (src/worker.rs)
   │
   ├── SUCCESS ─▶ (V1) queue.ack(91) → state='done'             ✅ terminal
   │
   └── FAILURE ─▶ (V3) retry::nack(...):                        (src/retry.rs)
          attempts=1, last_error=<reason>
          ├─ attempts < max_attempts → state='ready',
          │     run_at = now() + backoff(1)   → retried later
          └─ attempts == max_attempts → state='dead'  → DLQ    ⚰️ terminal
```

**Crash variant** (V2): if `worker-2` dies between `claim` and `ack`, job 91 sits
`running` with `locked_until` in the past. The [`reap_loop`](../src/lease.rs) sweep flips
it back to `ready`, and some *other* worker picks it up — at-least-once in action.

---

## 12. Mental model: what it *looks like* vs. what it *actually is*

| It looks like… | It actually is… |
|---|---|
| "Put a job on a queue" | `INSERT` a row into a Postgres table |
| A special queue server (RabbitMQ/SQS) | One `jobs` table; Postgres is store *and* broker |
| `SELECT` a job, then `UPDATE` it | A **race** — two workers grab the same row. Must be one atomic `FOR UPDATE SKIP LOCKED` statement |
| Claiming a job = owning it | A **lease** that expires — ownership you can lose by crashing |
| "The job ran, so it ran once" | **At-least-once**: it can run again if the ack was lost. Handlers must be idempotent |
| Retry = try again immediately | Exponential backoff **with jitter**, capped, then a **dead-letter queue** |
| A delayed job needs a scheduler thread | Just a `run_at` in the future + a claim that filters `run_at <= now()` |
| Workers poll the DB in a loop | Poll is the *fallback*; `LISTEN`/`NOTIFY` is the low-latency wake, poll stays the source of truth |
| "Exactly-once delivery" | A myth. At-least-once + idempotent effects is the real thing |

---

## 13. Where to look in the code

| Subtopic | File / symbol | Vertical |
|---|---|---|
| The table, columns, the `state` machine | [`migrations/0001_init.sql`](../migrations/0001_init.sql) | all |
| Job shapes (`Job`, `NewJob`, `JobState`) | [`src/job.rs`](../src/job.rs) | all |
| Enqueue + the atomic claim + ack | [`Queue::enqueue` / `claim` / `ack`](../src/queue.rs) | V1 |
| The partial index for the claim | TODO in [`migrations/0001_init.sql`](../migrations/0001_init.sql) | V1 |
| Lease reaper + heartbeat | [`reap_expired` / `reap_loop` / `extend_lease`](../src/lease.rs) | V2 |
| Backoff curve + retry/DLQ decision | [`RetryPolicy::backoff` / `nack`](../src/retry.rs) | V3 |
| Wakeup signal + poll fallback | [`wait_for_work` / `notify_ready`](../src/scheduler.rs) | V4 |
| The worker lifecycle (claim→run→ack/nack) | [`worker::run` / `process_one` / `handle`](../src/worker.rs) | wiring |
| Producer/admin HTTP API | [`src/routes.rs`](../src/routes.rs) | protocols |
| Error → HTTP mapping | [`src/error.rs`](../src/error.rs) | protocols |
| Wiring: pool, worker pool, graceful shutdown | [`src/main.rs`](../src/main.rs) | wiring |

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **Producer / enqueue** | Whoever puts a job on the queue (`POST /jobs`) |
| **Worker / consumer** | A process that claims and runs jobs |
| **Claim / dequeue** | Atomically take ownership of ready jobs |
| **`FOR UPDATE SKIP LOCKED`** | Postgres modifiers that let N workers claim different rows without blocking |
| **Lease / visibility timeout** | A time-boxed claim; expires so a crashed worker's job is retried |
| **Reaper** | Background sweep that returns expired-lease jobs to `ready` |
| **Ack / nack** | Mark a job done (ack) or handle its failure (nack) |
| **At-least-once** | Every job runs ≥ once, even across crashes — duplicates possible |
| **Idempotent** | Running the handler twice has the same effect as once |
| **Backoff + jitter** | Growing, randomized retry delays to avoid a retry stampede |
| **DLQ (dead-letter queue)** | Where jobs go after exhausting retries — set aside, inspectable |
| **Poison message** | A job that fails every time; the reason the DLQ exists |
| **`LISTEN` / `NOTIFY`** | Postgres pub/sub used as a best-effort "wake up, there's work" ping |
| **Poll fallback** | The slow periodic re-check that keeps the queue correct if a `NOTIFY` is lost |

---

## 15. Mental-model checklist (before you write code)

You're ready to start V1 when you can answer these without peeking:

1. Why is doing slow work *inside* a request bad — and what does moving it "behind the queue" buy?
2. Walk the double-dispatch race step by step. Why can't application code fix it?
3. What does `FOR UPDATE` lock, and what does `SKIP LOCKED` change about the *second* worker?
4. Why claim a *batch* instead of one row? Where does a claim-one worker spend its time?
5. What's a partial index, and why does it stay fast on a table that's 99% `done` jobs?
6. Why is a claim a *lease* and not permanent ownership? What does the reaper do?
7. Why is exactly-once impossible, and what makes at-least-once safe in practice?
8. Why does exponential backoff *without* jitter still cause a thundering herd?
9. What lands a job in the DLQ, and why must the DLQ be inspectable?
10. Why can't tuning the poll interval remove the latency-vs-load tradeoff — and why must the poll survive even after you add `NOTIFY`?

---

## 16. Suggested build order (from [`SPEC.md`](../SPEC.md))

1. **Boring path first** — `POST /jobs` inserts a row, `GET /jobs/{id}` reads it back. (First real `sqlx` queries; run `docker compose up -d`, `sqlx migrate run`, `cargo sqlx prepare`.)
2. **V1** — `FOR UPDATE SKIP LOCKED` batch claim + ack. Run **two** workers on a backlog; prove no job runs twice.
3. **V2** — make claims leases; add the reaper. Kill a worker mid-job; watch another pick it up.
4. **V3** — retry-with-backoff + DLQ. Enqueue an always-failing job; watch it back off, then dead-letter (not hot-loop).
5. **V4** — delayed jobs, then swap the poll-sleep for `LISTEN`/`NOTIFY`; measure the pickup-latency drop.
6. **Horizontal** — auth the enqueue API, cap the inputs, add the queue-depth / oldest-ready-age metrics, then benchmark and document.

---

## Further reading (concepts, not solutions)

- Postgres docs: `SELECT ... FOR UPDATE SKIP LOCKED`, and `LISTEN` / `NOTIFY`.
- Amazon SQS "visibility timeout" — where the lease idea and its name come from.
- AWS Architecture Blog: "Exponential Backoff and Jitter" (the canonical curve).
- "Postgres as a queue": Oban (Elixir), Graphile Worker, Solid Queue, good_job — production systems built on exactly this table.

Then read [`SPEC.md`](../SPEC.md) for the concrete challenges, and [`CONCEPTS.md`](../CONCEPTS.md)
for the "could you teach this at a whiteboard?" self-check on each idea.
</content>
</invoke>
