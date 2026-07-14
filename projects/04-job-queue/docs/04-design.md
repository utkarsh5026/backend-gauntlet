# 04 — Distributed Job Queue: Design Decisions

> Decision log for the choices the SPEC grades on. Each section is a mini
> decision record: **Context** (the forces) → **Options** → **Decision** (what
> you chose) → **Why** (the tradeoff you accepted). Fill the blanks as you build;
> raw benchmark numbers live in [`04-benchmarks.md`](./04-benchmarks.md), not here.
>
> This doc is the **Proof** for V2, V3, and V4 (see [`SPEC.md`](../SPEC.md)). A box
> in the SPEC flips to ✅ only once the reasoning it points at exists here. For the
> *concepts* behind these choices, see [`00-how-job-queues-work.md`](./00-how-job-queues-work.md).

---

## V1 — The claim query and its index

**Context.** N workers (across processes) claim from one `jobs` table without ever
running a row twice. Implemented in `src/queue.rs::claim`.

**The claim statement.** One atomic `UPDATE` (select + claim in a single statement —
no read-then-write race, so two workers can never both see the same row as `ready`):

```sql
UPDATE jobs
SET state        = 'running',
    locked_by    = $1,               -- worker id
    locked_at    = now(),
    locked_until = now() + make_interval(secs => $2)   -- the lease (V2)
WHERE id IN (
    SELECT id
    FROM jobs
    WHERE queue = $3 AND state = 'ready' AND run_at <= now()
    ORDER BY run_at                  -- oldest-first
    FOR UPDATE SKIP LOCKED           -- lock candidates; step over already-locked rows
    LIMIT $4                         -- claim a batch
)
RETURNING id, queue, kind, payload, state, attempts, max_attempts,
          run_at, locked_until, last_error;
```

The inner `SELECT ... FOR UPDATE SKIP LOCKED` locks the candidate rows; a second
concurrent claim `SKIP`s those locked rows rather than blocking on them, so N workers
partition the backlog instead of serialising on it. The outer `UPDATE` flips the same
rows to `running` and stamps the lease in the *same* statement/transaction, so the
lock is only ever held for the microseconds of the stamp — see the last note below.

- **Batch size** (`CLAIM_BATCH`): **10** (default, `main.rs`). One round-trip amortises
  the claim's network + planning cost across 10 jobs instead of paying it per job.
  Not *bigger* because a batch is claimed under one lease by one worker and worked
  **serially** — an over-large batch parks jobs behind that worker's queue (raising
  their latency) and, on a crash, dumps the whole batch back for redelivery after the
  lease expires. 10 balances round-trip amortisation against lease-holding granularity;
  it's an env knob, so a fast-handler / high-throughput deployment can raise it.
- **Ordering / filter:** confirmed — `queue = $3` (per-queue selector, the leading
  index column), `run_at <= now()` (only *due* jobs; future/delayed rows stay
  invisible), `ORDER BY run_at` (**oldest-first**). Oldest-first approximates FIFO and,
  more importantly, bounds the age of the oldest ready job — it's what keeps the
  `oldest_ready_age` lag metric from growing an unbounded tail under load.
- **The index** (`migrations/0002_claim_index.sql`):

  ```sql
  CREATE INDEX jobs_claim_idx ON jobs (queue, run_at) WHERE state = 'ready';
  ```
  - **Why partial (`WHERE state='ready'`)** vs a full index: only `ready` rows are ever
    claim candidates. `done`/`dead` rows are the ones that grow *without bound* (they're
    history), so a full index would bloat in lockstep with total throughput while adding
    nothing to the claim — every scan would still have to skip them. The partial index
    holds **only the live backlog**, so it stays small, cache-resident, and cheap to
    maintain: a row *leaves* the index when it's claimed (→ `running`) and *re-enters*
    when reaped/retried back to `ready`. It indexes the working set, not the archive.
  - **Why `(queue, run_at)` in that order:** the classic B-tree rule — equality column
    first (`queue`), then the range/sort column (`run_at`). That one order lets a single
    index scan serve the `queue =` seek, the `run_at <= now()` range, **and** the
    `ORDER BY run_at`, so `LIMIT 10` stops after 10 rows with **no sort node**.
  - `EXPLAIN` with vs. without → **38.1 ms → 0.32 ms, 5,483 → 53 buffers** over a
    20k-ready / 200k-done backlog. Numbers + plans in [`04-benchmarks.md`](./04-benchmarks.md) §V1.

**Why the lock lives inside a short transaction (stamp + commit, not hold-for-job):**
The `FOR UPDATE SKIP LOCKED` row locks are released the instant the claiming `UPDATE`
commits — we do **not** wrap the job's execution in that transaction. Exclusivity for
the *duration of the work* comes from the application-level **lease** (`state='running'`
+ `locked_until`), not from a held DB lock. Three reasons this matters:
1. **Connections.** A job can run for seconds-to-minutes; holding the transaction open
   would pin a pool connection (and an idle-in-transaction session) for that whole time,
   starving the pool. The stamp-and-commit claim frees the connection immediately.
2. **DB health.** A long-open transaction holds an old xmin — it blocks `VACUUM` from
   reclaiming dead tuples and bloats the table under a busy queue.
3. **Crash recovery is deterministic.** If exclusivity depended on the row lock, a
   crashed worker would only release it whenever Postgres happened to notice the dead
   connection — unpredictable. With the lease, recovery is a plain `locked_until < now()`
   sweep by the reaper (V2), on a timescale *we* choose. This is the exact V1↔V2 seam:
   the row lock is momentary contention-control for the hand-off; the lease is the
   durable, reap-able claim.

---

## V2 — Lease length + the at-least-once / idempotency story

**Context.** Each claim is a lease (`locked_until = now() + visibility`). A crashed
worker's job is reclaimed by the reaper (`src/lease.rs::reap_expired`).

**Lease length** (`VISIBILITY_TIMEOUT_SECS`): **30s** (default, `main.rs`).

| Too short | Too long |
|---|---|
| A healthy job that runs longer than the lease is reaped **while still running** → a second worker starts a **concurrent duplicate** (the expensive at-least-once failure — now two copies race, and any non-idempotent side effect happens twice). Also churns the reaper and inflates the redelivery rate. | After a **real crash**, the job sits `running` and unclaimable for the *whole* lease before anyone can reclaim it → slow recovery, and a fat tail on that job's end-to-end latency. Recovery time ≈ lease length. |

- **Decision + why:** 30s. The lease must comfortably exceed the *worst-case healthy*
  handler runtime (not the average) so live jobs are never reaped, yet stay short
  enough that crash recovery is ~tens of seconds. It's an env knob: a deployment with
  genuinely long handlers raises it (or uses the heartbeat, below). Concrete invariant
  in the code: `DEFAULT_EXEC_TIMEOUT` = **20s** (`handlers.rs`) is deliberately **< 30s
  lease**, so an `exec`/`shell` job is killed by its *own* timeout before its lease can
  expire — the reaper can never double-dispatch a still-running child process.
- **Reaper interval** (`REAPER_INTERVAL_SECS`): **10s** (default). Kept well below the
  lease (≈ ⅓) so recovery latency is dominated by the lease, not the sweep cadence:
  worst-case reclaim ≈ `lease + one interval` ≈ 30–40s. Making it *much* smaller just
  runs more empty sweeps against the table for no recovery win; larger and the sweep
  cadence starts adding to recovery time.
- **Does a reaped job count as a used attempt?** **No** — `reap_expired` only flips
  `running → ready` and clears the lease; it does **not** touch `attempts` (that's
  incremented solely in `retry::nack`, on an explicit handler failure). Rationale: a
  crash/timeout is usually an *infra* failure, not the job's fault, so it shouldn't burn
  the retry budget meant for job-level errors. **Known hole this opens:** a genuine
  *poison pill that kills its worker* (OOM, segfault, a `panic` that aborts the process)
  is reaped without ever incrementing `attempts`, so it **never dead-letters — it loops
  forever**. The DLQ (V3) only catches failures the handler *returns* as `Err`, not ones
  that take the worker down with them. Closing this needs a separate "delivery count"
  (bump on claim, dead-letter past a ceiling independent of `attempts`) — noted, not
  built.
- **Heartbeat (stretch)?** `extend_lease(pool, id, worker_id, by)` exists in `lease.rs`
  (owner-guarded, tested) so a slow-but-alive worker *can* push its own `locked_until`
  out — but it is **not yet wired into the worker loop** (`process_one` never calls it).
  So today a job longer than the lease relies on raising `VISIBILITY_TIMEOUT_SECS`; the
  heartbeat is available for a future long-job path but inert until the loop drives it.

**At-least-once is unavoidable. My idempotency strategy for handlers:**
Exactly-once is a myth here: a worker can finish a job and die *before* acking, so the
job runs again. The queue guarantees at-least-once; **safety under duplicate delivery is
the handler's contract**, not something the broker can provide.
- **Natural idempotency where possible:** the side-effect-free / observational kinds are
  safe by construction — `Noop`, `Sleep` (no external effect), `Echo` (re-printing is
  benign). An `Exec`/`Shell` whose command is itself idempotent (writes to a
  deterministic path, an `UPSERT`, a `DELETE ... WHERE`) is safe to re-run. This is the
  preferred design: make the effect converge, and duplicates stop mattering.
- **Idempotency-key dedup where not:** for effects that aren't naturally idempotent —
  the `Webhook` kind is the canonical example (a re-POST could double-charge / double-
  send) — the payload must carry a stable **idempotency key** that the *downstream*
  dedups on (e.g. an `Idempotency-Key` header à la Stripe, or a `processed(key)` unique
  row the handler inserts-or-skips before acting). Note this is **not implemented today**:
  the current `Webhook` handler sends no key, so it is *only* safe against a receiver
  that already dedups. That's the honest state — the strategy is the contract; the
  mechanism (a `dedup_key` column + a receiver-side check) is future work.
- **Concrete walk-through — `Webhook` duplicate:** worker A POSTs the webhook, the
  receiver processes it, then A crashes *before* `ack`. Its lease expires, the reaper
  returns the job to `ready`, worker B claims and POSTs **again**. With no key, the
  receiver sees the call twice; with an idempotency key in the request, the receiver
  recognises the retry and no-ops the second one. Same job, one durable outcome —
  achieved at the handler/receiver boundary, which is the only place it *can* be.

---

## V3 — Retry backoff curve + DLQ policy

**Context.** Failed jobs retry with backoff up to `max_attempts`, then dead-letter.
`src/retry.rs::backoff` + `nack`.

**The formula, as built** (`RetryPolicy::backoff`, `src/retry.rs:40`):

```
term = base_delay × 2^(attempt-1)                 # saturating; base_delay = 1s
if term > cap:            backoff = cap            # 300s exactly, no jitter
else:                     backoff = min(cap, term + U),   U ~ UniformInt[0s, cap)
```

- `base_delay` = **1s** (`BASE_DELAY`), `max_delay` (cap) = **300s / 5 min** (`MAX_DELAY`).
- **Jitter kind: additive full-window, sum clamped to the cap** — *not* AWS "full
  jitter". A fixed uniform offset `U ∈ [0s, 300s)` is added to the exponential term
  and the sum is clamped at 300s. (The window is constant, not proportional to the
  term — see the tradeoff below.) The clamp is the fix for the earlier overshoot bug;
  it's what makes `backoff_never_exceeds_max_delay` pass.

**Backoff curve** (default policy — the observable record the V3 Proof asks for):

| attempt `n` | nominal `2^(n-1)` | raw vs cap | backoff range (as built) | P(clamped to 300s) |
|--------:|------|-----------|--------------------------|--------------------|
| 1 | 1s | ≤ cap | `[1s, 300s]` | 1/300 ≈ 0.3% |
| 2 | 2s | ≤ cap | `[2s, 300s]` | 2/300 ≈ 0.7% |
| 3 | 4s | ≤ cap | `[4s, 300s]` | 4/300 ≈ 1.3% |
| 4 | 8s | ≤ cap | `[8s, 300s]` | 8/300 ≈ 2.7% |
| 5 | 16s | ≤ cap | `[16s, 300s]` | 16/300 ≈ 5.3% |
| 6 | 32s | ≤ cap | `[32s, 300s]` | 32/300 ≈ 11% |
| 7 | 64s | ≤ cap | `[64s, 300s]` | 64/300 ≈ 21% |
| 8 | 128s | ≤ cap | `[128s, 300s]` | 128/300 ≈ 43% |
| 9 | 256s | ≤ cap | `[256s, 300s]` | 256/300 ≈ 85% |
| ≥10 | ≥512s | > cap | `300s` exactly (jitter skipped) | 100% |

> **Reachable range at the default budget.** With `max_attempts = 5`, a job dead-letters
> on its 5th failure, so only **rows 1–4** ever schedule a real reschedule
> (`backoff(1..4)`, nominal 1–8s). Rows 5–10 document the curve's shape and cap
> behaviour, and only bite if the budget is raised.

**Tradeoff accepted (the reason to not stop here).** Because the jitter window is a
constant `[0, 300s)` rather than proportional to the term, the clamp puts a growing
**point mass at the 300s ceiling** (last column): by attempt 9, ~85% of retries fire at
*exactly* 300s, re-synchronising the herd — the very thing jitter exists to prevent.
It's correct and provably capped, just weakly decorrelated at high attempt counts. At
the default `max_attempts = 5` (≤ 2.7% clamp) this is negligible; it only matters if the
budget is pushed past ~8. The proportional-window shape (AWS full jitter,
`random(0, min(cap, term))`) avoids the point mass — a candidate refinement, noted, not
required by the SPEC.

**DLQ policy.**
- **Representation:** terminal `state = 'dead'` in the same `jobs` table (no separate
  table). Set by `nack` when `attempts >= max_attempts`; the `state` enum lives in
  `migrations/0001_init.sql` + `job.rs::JobState`. *Why:* one table keeps the claim,
  lease, retry, and DLQ transitions as single-row `UPDATE`s — no cross-table move, and
  a dead row is still visible to the same `GET /jobs/{id}` path.
- **Error classification:** **none yet** — `nack` retries *every* failure uniformly
  until the budget is spent, so a permanent error (bad payload) burns all attempts
  before dead-lettering. Known simplification; the transient-vs-permanent split is still
  an open decision (see `03-retries-backoff-dlq.md` §"Classify errors").
- **Inspect + requeue surface: OPEN / not yet built.** `routes.rs` exposes only
  `POST /jobs` and `GET /jobs/{id}` — there is no list-dead / requeue endpoint. The V3
  "DLQ is inspectable and requeueable" box **stays unchecked** until this exists.

**Proof** (all green — `cargo test -p job-queue --bin job-queue retry::tests`):
- Curve contract (pure): `backoff_caps_large_attempts_at_max_delay`,
  `backoff_first_retry_waits_at_least_base_delay`, `backoff_applies_jitter`,
  `backoff_never_exceeds_max_delay` → exponential **+** jittered **+** capped.
- Failure lifecycle (`#[sqlx::test]`): `nack_reschedules_with_remaining_attempts`
  (attempts remain → `ready`, `run_at` pushed out), `nack_dead_letters_when_attempts_exhausted`
  (last attempt → `dead`, terminal), `nack_poison_message_reaches_dlq_and_stops` (an
  always-failing job retries to the cap then stops in the DLQ — no hot loop).

---

## V4 — LISTEN/NOTIFY design (and why the poll survives)

**Context.** Replace the idle-worker poll-sleep with an event wakeup, keeping the
durable poll as source of truth. `src/scheduler.rs::wait_for_work` + `notify_ready`.

- **Channel naming** (one place both listen & notify agree): `__________`
- **Who NOTIFYs:** enqueue? retry-coming-due? delay-coming-due? `__________`
- **Poll fallback interval:** `____` — why the poll is **not** optional: `__________`
- **Delayed jobs** (`run_at` in the future): what wakes a worker when the delay expires? `__________`
- **The design principle in one line:** _(durable pull for truth, ephemeral push for latency)_ `__________`

---

## Horizontal decisions

- **Auth on `POST /jobs`:** scheme (API key / token), where enforced: `__________`
- **Input caps:** payload size `____`, `queue`/`kind` charset+length `____`, `max_attempts` ceiling `____`, `delay` ceiling `____`
- **Graceful shutdown order:** _(stop claiming → drain in-flight / let leases lapse → exit)_ `__________`
- **Pool vs worker count:** `DB_MAX_CONNECTIONS`=`____`, `WORKER_CONCURRENCY`=`____` — why they're tuned together: `__________`
- **Key metrics wired:** queue depth (`ready`), in-flight (`running`), DLQ size, **oldest-ready-age** (the lag metric), leases-reaped rate, enqueue/complete/retry/dead counters, exec-time + end-to-end latency histograms. Where exposed: `__________`
</content>
