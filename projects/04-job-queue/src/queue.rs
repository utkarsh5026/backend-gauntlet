//! V1 — The claim engine: enqueue + the `SKIP LOCKED` dequeue, from scratch.
//!
//! This is the piece you'd normally get from a broker (RabbitMQ / SQS / Sidekiq).
//! `enqueue` is a plain `INSERT`; the learning is in `claim`, the **atomic
//! dequeue** that hands each job to exactly one worker even when N workers race.
//!
//! The trap is the read-then-write race: `SELECT ... LIMIT 1` then `UPDATE`
//! double-dispatches, because two workers read the same row before either claims
//! it. The fix is to select and claim in **one** statement —
//! `SELECT ... FOR UPDATE SKIP LOCKED` (so a second worker steps over rows the
//! first already locked instead of blocking on them).

use std::sync::Arc;
use std::time::Duration;

use chrono::{Duration as ChronoDuration, Utc};
use sqlx::PgPool;

use crate::error::AppError;
use crate::job::{Job, JobId, JobState, NewJob};
use crate::scheduler;

/// Handle to the `jobs` table — the public surface of the V1 claim engine.
///
/// A `Queue` is a thin wrapper over a Postgres [`PgPool`] plus the fallback
/// `max_attempts` for jobs that don't set their own. It is always held behind an
/// [`Arc`] (see [`Queue::new`]) so the same instance can back both the request
/// handlers and the worker pool via `AppState` without further wrapping.
pub struct Queue {
    pool: PgPool,
    default_max_attempts: i32,
}

impl Queue {
    /// Build a [`Queue`] over `pool`, returned in an [`Arc`] for cheap sharing.
    ///
    /// `default_max_attempts` fills in the attempt budget for any [`NewJob`]
    /// that doesn't override it. The `Arc` lets one queue be cloned into every
    /// worker task and request handler at near-zero cost.
    pub fn new(pool: PgPool, default_max_attempts: i32) -> Arc<Self> {
        Arc::new(Self {
            pool,
            default_max_attempts,
        })
    }

    /// Borrow the underlying connection pool.
    ///
    /// Exposed so the sibling verticals that run their own SQL against the same
    /// database (the visibility-timeout reaper in `lease.rs`, retry/DLQ moves)
    /// can reuse this pool instead of opening a second one.
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Insert a new job and return its freshly allocated [`JobId`].
    ///
    /// A plain `INSERT` — the row lands in state `ready`. `max_attempts` falls
    /// back to the queue default when the request omits it, and `delay_secs` is
    /// clamped to `>= 0` and added to `now()` to compute `run_at` (so a job can
    /// be scheduled into the future but never into the past). Until `run_at` is
    /// due, [`claim`](Self::claim) won't hand the job out.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::Db`] if the `INSERT` fails (e.g. the queue is
    /// unreachable or a constraint is violated).
    pub async fn enqueue(&self, new: NewJob) -> Result<JobId, AppError> {
        let max_attempts = new.max_attempts.unwrap_or(self.default_max_attempts);
        let delay_secs = new.delay_secs.unwrap_or(0).max(0);
        let run_at = Utc::now() + ChronoDuration::seconds(delay_secs);

        let id: JobId = sqlx::query_scalar!(
            r#"
            INSERT INTO jobs (queue, kind, payload, max_attempts, run_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            "#,
            new.queue,
            new.kind,
            new.payload,
            max_attempts,
            run_at,
        )
        .fetch_one(&self.pool)
        .await?;

        if let Err(e) = scheduler::notify_ready(&self.pool, &new.queue).await {
            tracing::warn!(error = %e, queue = %new.queue, "NOTIFY failed; poll will catch up");
        }

        metrics::counter!(crate::metrics::ENQUEUED_TOTAL, "queue" => new.queue).increment(1);
        Ok(id)
    }

    /// Atomically claim up to `limit` due jobs from `queue` for `worker_id`.
    ///
    /// This is the heart of the vertical. In a single statement it selects the
    /// oldest `ready`, due (`run_at <= now()`) rows with `FOR UPDATE SKIP
    /// LOCKED` and flips them to `running`, stamping `locked_by = worker_id` and
    /// a lease `locked_until = now() + visibility`. Doing the select and the
    /// claim as one statement is what makes it safe under concurrency: a second
    /// worker steps over rows this call already locked instead of racing to
    /// re-read them, so no job is ever dispatched twice (see the module docs for
    /// the read-then-write trap this avoids).
    ///
    /// The `visibility` duration is the lease window: if the worker crashes
    /// before acking, the reaper (V2) can reclaim the job once `locked_until`
    /// passes. Returns the claimed [`Job`]s (already in state `running`); an
    /// empty vec means nothing was due.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::Db`] if the claiming `UPDATE` fails.
    pub async fn claim(
        &self,
        queue: &str,
        worker_id: &str,
        limit: i64,
        visibility: Duration,
    ) -> Result<Vec<Job>, AppError> {
        let visibility_secs = visibility.as_secs_f64();
        let jobs = sqlx::query_as!(
            Job,
            r#"
            UPDATE jobs
            SET
                state = 'running',
                locked_by = $1,
                locked_at = now(),
                locked_until = now() + make_interval(secs => $2)
            WHERE id IN (
                SELECT id
                FROM jobs
                WHERE queue = $3 AND state = 'ready' AND run_at <= now()
                ORDER BY run_at
                FOR UPDATE SKIP LOCKED
                LIMIT $4
            )
            RETURNING
                id,
                queue,
                kind,
                payload,
                state as "state: JobState",
                attempts,
                max_attempts,
                run_at,
                locked_until,
                last_error
            "#,
            worker_id,
            visibility_secs,
            queue,
            limit,
        )
        .fetch_all(&self.pool)
        .await?;

        if jobs.is_empty() {
            metrics::counter!(crate::metrics::CLAIMS_EMPTY_TOTAL, "queue" => queue.to_string())
                .increment(1);
        }

        Ok(jobs)
    }

    /// Mark a job `done` and release its lease — the worker's success path.
    ///
    /// Sets `state = 'done'` and clears `locked_by` / `locked_at` /
    /// `locked_until`, so the job is retired and never claimed again. A no-op if
    /// no row has that `id`.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::Db`] if the `UPDATE` fails.
    pub async fn ack(&self, id: JobId) -> Result<(), AppError> {
        sqlx::query!(
            r#"
            UPDATE jobs SET state = 'done', locked_by = NULL, locked_at = NULL, locked_until = NULL
            WHERE id = $1
            "#,
            id,
        )
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// Fetch a job by `id`, or `None` if no such job exists.
    ///
    /// A read-only lookup backing the admin `GET /jobs/{id}` route: a missing id
    /// maps to `None` (a 404), any state maps to the full [`Job`] row.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::Db`] if the `SELECT` fails.
    pub async fn get(&self, id: JobId) -> Result<Option<Job>, AppError> {
        let job = sqlx::query_as!(
            Job,
            r#"
            SELECT
                id,
                queue,
                kind,
                payload,
                state as "state: JobState",
                attempts,
                max_attempts,
                run_at,
                locked_until,
                last_error
            FROM jobs
            WHERE id = $1
            "#,
            id,
        )
        .fetch_optional(&self.pool)
        .await?;

        Ok(job)
    }

    /// List dead-lettered jobs, newest-first, one page at a time.
    ///
    /// Ordered by `id DESC` — a monotonic, stable key, so `OFFSET` pages don't
    /// overlap or skip. (`updated_at` is *not* maintained past insert, so it can't
    /// serve as a "died at" sort; `id DESC` is the honest newest-first proxy.)
    /// The caller's `limit`/`offset` are expected to arrive already clamped at the
    /// HTTP boundary (see `routes::get_dlq`) — this method trusts them.
    ///
    /// `LIMIT`/`OFFSET` is fine for an admin-facing DLQ that's rarely huge; note the
    /// tradeoff that a deep `OFFSET` re-scans all skipped rows (O(offset)), where a
    /// keyset (`WHERE id < $last_seen`) would not. Not worth the complexity here.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::Db`] if the `SELECT` fails.
    pub async fn get_dlq(&self, limit: i64, offset: i64) -> Result<Vec<Job>, AppError> {
        let jobs = sqlx::query_as!(
            Job,
            r#"
            SELECT
                id,
                queue,
                kind,
                payload,
                state as "state: JobState",
                attempts,
                max_attempts,
                run_at,
                locked_until,
                last_error
            FROM jobs
            WHERE state = 'dead'
            ORDER BY id DESC
            LIMIT $1 OFFSET $2
            "#,
            limit,
            offset,
        )
        .fetch_all(&self.pool)
        .await?;
        Ok(jobs)
    }

    pub async fn requeue(&self, id: JobId) -> Result<Option<Job>, AppError> {
        let job = sqlx::query_as!(
            Job,
            r#"
            UPDATE jobs SET state = 'ready', attempts = 0, run_at = now() WHERE id = $1 AND state = 'dead'
            RETURNING
                id,
                queue,
                kind,
                payload,
                state as "state: JobState",
                attempts,
                max_attempts,
                run_at,
                locked_until,
                last_error
            "#,
            id,
        )
        .fetch_optional(&self.pool)
        .await?;
        Ok(job)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::collections::HashSet;

    use serde_json::json;
    use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

    use crate::retry::{self, Disposition, RetryPolicy};

    const VIS: Duration = Duration::from_secs(30);

    /// A minimal `NewJob` on `queue` with no overrides — the common case.
    fn new_job(queue: &str) -> NewJob {
        NewJob {
            queue: queue.to_string(),
            kind: "send_email".into(),
            payload: json!({ "to": "a@b.com" }),
            max_attempts: None,
            delay_secs: None,
        }
    }

    /// A fresh enqueue lands a `ready`, un-attempted row that round-trips the
    /// queue/kind/payload verbatim and returns a positive BIGSERIAL id.
    #[sqlx::test]
    async fn enqueue_inserts_ready_job_and_returns_id(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5);

        let id = q.enqueue(new_job("emails")).await.expect("enqueue");
        assert!(id > 0, "BIGSERIAL id should be positive, got {id}");

        let row = sqlx::query!(
            "SELECT queue, kind, payload, state, attempts FROM jobs WHERE id = $1",
            id
        )
        .fetch_one(&pool)
        .await
        .expect("row should exist");

        assert_eq!(row.queue, "emails");
        assert_eq!(row.kind, "send_email");
        assert_eq!(row.payload, json!({ "to": "a@b.com" }));
        assert_eq!(row.state, "ready", "a new job starts ready");
        assert_eq!(row.attempts, 0, "no attempts have run yet");
    }

    /// An explicit `max_attempts` on the request wins over the queue default.
    #[sqlx::test]
    async fn enqueue_uses_explicit_max_attempts_over_default(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5); // default we expect to be overridden

        let mut job = new_job("emails");
        job.max_attempts = Some(2);
        let id = q.enqueue(job).await.expect("enqueue");

        let row = sqlx::query!("SELECT max_attempts FROM jobs WHERE id = $1", id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(row.max_attempts, 2);
    }

    /// With no per-job override, the row takes the queue's default max_attempts.
    #[sqlx::test]
    async fn enqueue_falls_back_to_default_max_attempts(pool: PgPool) {
        let q = Queue::new(pool.clone(), 7);

        let id = q.enqueue(new_job("emails")).await.expect("enqueue"); // max_attempts: None

        let row = sqlx::query!("SELECT max_attempts FROM jobs WHERE id = $1", id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(row.max_attempts, 7);
    }

    /// `delay_secs` pushes `run_at` into the future by that many seconds, so the
    /// job isn't claimable until it's due (V4's delayed delivery).
    #[sqlx::test]
    async fn enqueue_with_delay_sets_future_run_at(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5);

        let delay = 60i64;
        let mut job = new_job("emails");
        job.delay_secs = Some(delay);

        // enqueue stamps run_at = now() + delay for some now() in [before, after].
        let before = Utc::now();
        let id = q.enqueue(job).await.expect("enqueue");
        let after = Utc::now();

        let row = sqlx::query!("SELECT run_at FROM jobs WHERE id = $1", id)
            .fetch_one(&pool)
            .await
            .unwrap();

        // Postgres TIMESTAMPTZ has microsecond resolution, so the stored run_at is
        // truncated below the nanosecond-precision `before` — slacken the lower
        // bound by a hair so the sub-µs drop can't make this flake.
        let lo = before + ChronoDuration::seconds(delay) - ChronoDuration::milliseconds(1);
        let hi = after + ChronoDuration::seconds(delay);
        assert!(
            row.run_at >= lo && row.run_at <= hi,
            "run_at {} should be ~{delay}s ahead, in [{lo}, {hi}]",
            row.run_at,
        );
    }

    /// A negative `delay_secs` is clamped to 0 (`.max(0)`), so `run_at` is ~now —
    /// never scheduled in the past, which would make the job instantly overdue.
    #[sqlx::test]
    async fn enqueue_clamps_negative_delay_to_now(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5);

        let mut job = new_job("emails");
        job.delay_secs = Some(-100);

        let before = Utc::now();
        let id = q.enqueue(job).await.expect("enqueue");
        let after = Utc::now();

        let row = sqlx::query!("SELECT run_at FROM jobs WHERE id = $1", id)
            .fetch_one(&pool)
            .await
            .unwrap();
        // `before - 1ms` absorbs Postgres's microsecond truncation of run_at; the
        // real regression this guards against (a -100s delay leaking through) is
        // 100_000ms out, so the slack can't mask it.
        let lo = before - ChronoDuration::milliseconds(1);
        assert!(
            row.run_at >= lo && row.run_at <= after,
            "clamped run_at {} should be ~now, in [{lo}, {after}], not in the past",
            row.run_at,
        );
    }

    /// A claimed job comes back exactly once: it is flipped to `running`, stamped
    /// with a future lease, and — because it is no longer `ready` — a second claim
    /// finds nothing.
    #[sqlx::test]
    async fn claim_returns_ready_job_once_then_empty(pool: PgPool) {
        let q = Queue::new(pool, 5);

        let id = q.enqueue(new_job("emails")).await.expect("enqueue");

        let before = Utc::now();
        let claimed = q.claim("emails", "w1", 10, VIS).await.expect("claim");
        assert_eq!(claimed.len(), 1, "the one ready job should be claimed");

        let job = &claimed[0];
        assert_eq!(job.id, id);
        assert_eq!(job.state, JobState::Running, "claim flips ready -> running");
        let lease = job.locked_until.expect("claim stamps a lease");
        assert!(
            lease > before && lease <= Utc::now() + ChronoDuration::seconds(31),
            "lease {lease} should be ~{}s out",
            VIS.as_secs(),
        );

        let again = q.claim("emails", "w2", 10, VIS).await.expect("claim");
        assert!(again.is_empty(), "a running job is not claimable again");
    }

    /// `claim` never returns more than `limit` rows, so a backlog is drained in
    /// batches: 5 ready jobs at limit=2 come out 2, 2, 1, then empty.
    #[sqlx::test]
    async fn claim_respects_limit_batch(pool: PgPool) {
        let q = Queue::new(pool, 5);

        for _ in 0..5 {
            q.enqueue(new_job("emails")).await.expect("enqueue");
        }

        let mut sizes = Vec::new();
        loop {
            let batch = q.claim("emails", "w1", 2, VIS).await.expect("claim");
            if batch.is_empty() {
                break;
            }
            sizes.push(batch.len());
        }
        assert_eq!(sizes, vec![2, 2, 1], "limit=2 over 5 jobs drains 2,2,1");
    }

    /// A job whose `run_at` is in the future is invisible to `claim` until due —
    /// only the immediately-ready job on the same queue comes back.
    #[sqlx::test]
    async fn claim_skips_jobs_not_yet_due(pool: PgPool) {
        let q = Queue::new(pool, 5);

        let mut future = new_job("emails");
        future.delay_secs = Some(3600); // an hour out
        let future_id = q.enqueue(future).await.expect("enqueue future");
        let ready_id = q.enqueue(new_job("emails")).await.expect("enqueue ready");

        let claimed = q.claim("emails", "w1", 10, VIS).await.expect("claim");
        let ids: Vec<JobId> = claimed.iter().map(|j| j.id).collect();
        assert_eq!(ids, vec![ready_id], "only the due job is claimed");
        assert!(!ids.contains(&future_id), "the future job stays invisible");
    }

    /// `claim` is scoped to its queue: a job on another queue is never handed out,
    /// even when both are ready.
    #[sqlx::test]
    async fn claim_is_scoped_to_its_queue(pool: PgPool) {
        let q = Queue::new(pool, 5);

        q.enqueue(new_job("alpha")).await.expect("enqueue a");
        q.enqueue(new_job("beta")).await.expect("enqueue b");

        let claimed = q.claim("alpha", "w1", 10, VIS).await.expect("claim");
        assert_eq!(claimed.len(), 1);
        assert_eq!(
            claimed[0].queue, "alpha",
            "only queue alpha's job is claimed"
        );
    }

    /// `ack` retires a claimed job to `done` and clears its lease, so it can never
    /// be claimed again — the worker's success path.
    #[sqlx::test]
    async fn ack_marks_job_done_and_unclaimable(pool: PgPool) {
        let q = Queue::new(pool, 5);

        let id = q.enqueue(new_job("emails")).await.expect("enqueue");
        let claimed = q.claim("emails", "w1", 10, VIS).await.expect("claim");
        assert_eq!(claimed.len(), 1);

        q.ack(id).await.expect("ack");

        let job = q.get(id).await.expect("get").expect("job exists");
        assert_eq!(job.state, JobState::Done, "ack -> done");
        assert!(job.locked_until.is_none(), "ack clears the lease");

        let again = q.claim("emails", "w2", 10, VIS).await.expect("claim");
        assert!(again.is_empty(), "a done job is never re-claimed");
    }

    /// `get` round-trips a stored job and returns `None` for an id that never
    /// existed (backs `GET /jobs/{id}` -> 404).
    #[sqlx::test]
    async fn get_returns_job_or_none(pool: PgPool) {
        let q = Queue::new(pool, 5);

        let id = q.enqueue(new_job("emails")).await.expect("enqueue");
        let got = q.get(id).await.expect("get").expect("job exists");
        assert_eq!(got.id, id);
        assert_eq!(got.state, JobState::Ready);
        assert_eq!(got.queue, "emails");

        assert!(
            q.get(-1).await.expect("get missing").is_none(),
            "a non-existent id yields None"
        );
    }

    /// The SKIP LOCKED guarantee under contention: N workers racing over a backlog
    /// of M jobs claim M **distinct** jobs in total — no job is ever handed to two
    /// workers. Each worker drains in batches until it sees an empty claim; because
    /// every claim flips its rows to `running` in one committed statement, the
    /// backlog strictly shrinks and all workers terminate.
    ///
    /// This one takes the pool *options* rather than a ready `PgPool` so it can
    /// size the pool for real parallelism (the injected default pool is small),
    /// while `#[sqlx::test]` still points those options at the fresh per-test DB.
    #[sqlx::test]
    async fn concurrent_claimers_never_double_dispatch(
        opts: PgPoolOptions,
        connect: PgConnectOptions,
    ) {
        const WORKERS: usize = 6;
        const JOBS: usize = 60;
        const BATCH: i64 = 5;

        // Size the pool to the worker count so the N claimers genuinely overlap.
        let pool = opts
            .max_connections(WORKERS as u32 + 2)
            .connect_with(connect)
            .await
            .expect("pool");
        let q = Queue::new(pool, 5);

        for _ in 0..JOBS {
            q.enqueue(new_job("emails")).await.expect("enqueue");
        }

        let queue = "emails";
        let mut handles = Vec::new();
        for w in 0..WORKERS {
            let q = q.clone();
            handles.push(tokio::spawn(async move {
                let worker_id = format!("w{w}");
                let mut claimed = Vec::new();
                loop {
                    let batch = q.claim(queue, &worker_id, BATCH, VIS).await.expect("claim");
                    if batch.is_empty() {
                        break;
                    }
                    claimed.extend(batch.into_iter().map(|j| j.id));
                }
                claimed
            }));
        }

        let mut all = Vec::new();
        for h in handles {
            all.extend(h.await.expect("worker task"));
        }

        let unique: HashSet<JobId> = all.iter().copied().collect();
        assert_eq!(
            all.len(),
            unique.len(),
            "no job claimed twice: {} claims but only {} distinct ids",
            all.len(),
            unique.len(),
        );
        assert_eq!(unique.len(), JOBS, "every job is claimed exactly once");
    }

    // ---- DLQ: inspect + requeue (V3.4 / horizontal API) -----------------------

    /// Drive one job all the way to `dead`: enqueue it one-shot (`max_attempts = 1`),
    /// claim it, then `nack` it once — which dead-letters it (attempts exhausted).
    /// Returns its id, the DLQ fixture the requeue tests act on.
    async fn dead_letter_one(q: &Queue, pool: &PgPool, queue_name: &str) -> JobId {
        let mut job = new_job(queue_name);
        job.max_attempts = Some(1);
        let id = q.enqueue(job).await.expect("enqueue");

        let claimed = q.claim(queue_name, "w1", 10, VIS).await.expect("claim");
        let running = claimed
            .into_iter()
            .find(|j| j.id == id)
            .expect("the one-shot job was claimed");

        let disp = retry::nack(pool, &RetryPolicy::default(), &running, "boom")
            .await
            .expect("nack");
        assert_eq!(
            disp,
            Disposition::DeadLettered,
            "a one-shot job dead-letters on its first failure",
        );
        id
    }

    /// The V3.4 headline: a dead job is **inspectable** in the DLQ and **requeueable**
    /// back to a fresh, claimable life. Drive a job to `dead`, confirm the DLQ lists
    /// it, requeue it, and assert it returns to `ready` with a reset attempt budget,
    /// leaves the DLQ, and is claimed again.
    #[sqlx::test]
    async fn requeue_revives_a_dead_job_with_a_fresh_budget(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5);
        let id = dead_letter_one(&q, &pool, "emails").await;

        // Inspectable: it shows up in the DLQ listing, and `get` confirms the state.
        let dlq = q.get_dlq(50, 0).await.expect("get_dlq");
        assert!(
            dlq.iter().any(|j| j.id == id),
            "the dead job appears in the DLQ"
        );
        let dead = q.get(id).await.expect("get").expect("job exists");
        assert_eq!(dead.state, JobState::Dead);
        assert_eq!(dead.attempts, 1, "the exhausted attempt is recorded");

        // Requeueable: dead -> ready with the attempt budget reset to 0.
        let revived = q
            .requeue(id)
            .await
            .expect("requeue")
            .expect("a dead job is requeued");
        assert_eq!(
            revived.state,
            JobState::Ready,
            "requeue returns it to ready"
        );
        assert_eq!(revived.attempts, 0, "requeue resets the attempt budget");

        // ...gone from the DLQ, and claimable again for a fresh run.
        let dlq_after = q.get_dlq(50, 0).await.expect("get_dlq");
        assert!(
            !dlq_after.iter().any(|j| j.id == id),
            "a requeued job leaves the DLQ"
        );
        let claimed = q.claim("emails", "w2", 10, VIS).await.expect("claim");
        assert!(
            claimed.iter().any(|j| j.id == id),
            "the requeued job is claimable again"
        );
    }

    /// The `state = 'dead'` guard: `requeue` acts only on dead jobs. A `ready` job, a
    /// `running` (leased) job, and an unknown id all return `None` (the route maps
    /// that to 404) — so the admin door can never resurrect a live or completed job
    /// into a double-run.
    #[sqlx::test]
    async fn requeue_ignores_non_dead_and_unknown_jobs(pool: PgPool) {
        let q = Queue::new(pool, 5);
        let id = q.enqueue(new_job("emails")).await.expect("enqueue");

        // ready: not requeueable, and left untouched.
        assert!(
            q.requeue(id).await.expect("requeue").is_none(),
            "a ready job is not requeued"
        );
        assert_eq!(
            q.get(id).await.expect("get").expect("exists").state,
            JobState::Ready,
        );

        // running (leased): still not requeueable — this is the double-run guard.
        let claimed = q.claim("emails", "w1", 10, VIS).await.expect("claim");
        assert_eq!(claimed[0].id, id, "claimed the same job");
        assert!(
            q.requeue(id).await.expect("requeue").is_none(),
            "a running job is not stolen back to ready"
        );
        assert_eq!(
            q.get(id).await.expect("get").expect("exists").state,
            JobState::Running,
            "the in-flight job stays running",
        );

        // an id that never existed is None (-> 404), not an error.
        assert!(
            q.requeue(-1).await.expect("requeue").is_none(),
            "an unknown id yields None, not an error"
        );
    }

    /// `get_dlq` pages **newest-first** (`id DESC`) and honours `limit`/`offset`: with
    /// five dead jobs, `limit = 2` returns the two highest ids and `offset` walks down
    /// the id-desc order without overlap. (The route layer additionally *clamps* the
    /// caller's limit into `[1, MAX]` — that boundary cap is enforced in
    /// `routes::get_dlq`, not this data method, which trusts its args.)
    #[sqlx::test]
    async fn get_dlq_paginates_newest_first(pool: PgPool) {
        let q = Queue::new(pool.clone(), 5);

        let mut ids = Vec::new();
        for _ in 0..5 {
            ids.push(dead_letter_one(&q, &pool, "emails").await);
        }
        let mut newest_first = ids.clone();
        newest_first.sort_unstable_by(|a, b| b.cmp(a)); // id DESC

        let page1: Vec<JobId> = q
            .get_dlq(2, 0)
            .await
            .expect("dlq")
            .iter()
            .map(|j| j.id)
            .collect();
        assert_eq!(
            page1,
            newest_first[0..2],
            "first page is the two newest dead jobs, id desc",
        );

        let page2: Vec<JobId> = q
            .get_dlq(2, 2)
            .await
            .expect("dlq")
            .iter()
            .map(|j| j.id)
            .collect();
        assert_eq!(
            page2,
            newest_first[2..4],
            "offset walks down the id-desc order, no overlap",
        );

        assert_eq!(
            q.get_dlq(50, 0).await.expect("dlq").len(),
            5,
            "all five dead jobs are listed within the page limit",
        );
    }
}
