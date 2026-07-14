//! V2 — Visibility timeout / lease: at-least-once delivery.
//!
//! A claim isn't "this job is done" — it's "this worker may *try* for a while."
//! Each claim stamps `locked_until = now() + lease` (done in `queue::claim`).
//! If the worker acks before then, great. If it crashes, the job sits `running`
//! with an expired lease — and the **reaper** here returns it to `ready` so
//! another worker retries it. That sweep is the whole reason a crashed worker
//! doesn't lose its job.
//!
//! The cost you must accept: a worker can finish a job and die *before* acking,
//! so the job runs again. There is no free exactly-once — the answer is
//! **idempotent handlers**. The lease length is a real tradeoff (too short →
//! spurious double-runs of slow jobs; too long → slow crash recovery).

use std::time::Duration;

use sqlx::PgPool;
use tokio::sync::watch;
use tracing::{debug, error, info};

use crate::error::AppError;

/// Find `running` jobs whose lease (`locked_until`) has passed and make them
/// claimable again. Returns how many were requeued.
pub async fn reap_expired(pool: &PgPool) -> Result<u64, AppError> {
    let rows = sqlx::query!(
        r#"
        UPDATE jobs SET state='ready', locked_by=NULL, locked_until=NULL
        WHERE state='running' AND locked_until < now()
        "#,
    )
    .execute(pool)
    .await?;
    let requeued = rows.rows_affected();
    if requeued > 0 {
        metrics::counter!(crate::metrics::LEASES_REAPED_TOTAL).increment(requeued);
    }
    Ok(requeued)
}

pub async fn reap_loop(pool: PgPool, interval: Duration, mut shutdown: watch::Receiver<bool>) {
    info!(?interval, "lease reaper started");
    let mut ticker = tokio::time::interval(interval);
    loop {
        tokio::select! {
            _ = ticker.tick() => match reap_expired(&pool).await {
                Ok(0) => {}
                Ok(n) => info!(requeued = n, "reaped expired job leases"),
                Err(e) => error!(error = %e, "reaper sweep failed"),
            },
            _ = shutdown.changed() => {
                debug!("lease reaper shutting down");
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    use serde_json::json;

    use crate::job::{Job, JobId, JobState, NewJob};
    use crate::queue::Queue;

    /// A comfortable lease: long enough that a job stays exclusively its holder's
    /// for the duration of a test unless we deliberately expire it.
    const LEASE: Duration = Duration::from_secs(30);
    const QUEUE_NAME: &str = "emails";
    const DEFAULT_MAX_ATTEMPTS: i32 = 5;

    fn queue(pool: &PgPool) -> Arc<Queue> {
        Queue::new(pool.clone(), DEFAULT_MAX_ATTEMPTS)
    }

    fn new_job() -> NewJob {
        NewJob {
            queue: QUEUE_NAME.to_string(),
            kind: "send_email".into(),
            payload: json!({ "to": "a@b.com" }),
            max_attempts: None,
            delay_secs: None,
        }
    }

    async fn claim_job(q: &Queue, worker_id: &str, limit: i64) -> Vec<Job> {
        q.claim(QUEUE_NAME, worker_id, limit, LEASE)
            .await
            .expect("claim")
    }

    async fn enqueue_job(q: &Queue) -> JobId {
        q.enqueue(new_job()).await.expect("enqueue")
    }

    async fn get_job(q: &Queue, id: JobId) -> Job {
        q.get(id).await.expect("get").expect("job exists")
    }

    /// Fast-forward a claimed job's lease into the past so the reaper sees it as
    /// expired — the deterministic stand-in for a crashed / too-slow worker.
    /// Unchecked `query` (not `query!`) keeps this test-only statement out of the
    /// committed sqlx offline cache.
    async fn expire_lease(pool: &PgPool, id: JobId) {
        sqlx::query("UPDATE jobs SET locked_until = now() - interval '1 minute' WHERE id = $1")
            .bind(id)
            .execute(pool)
            .await
            .expect("backdate lease into the past");
    }

    /// The core at-least-once guarantee: a worker claims a job then "dies" (never
    /// acks); once its lease lapses the reaper returns the job to `ready`, clears
    /// the stale lease, and a *second* worker reclaims the same job.
    #[sqlx::test]
    async fn reap_requeues_expired_lease_and_frees_it_for_reclaim(pool: PgPool) {
        let q = queue(&pool);
        let id = enqueue_job(&q).await;

        let claimed = claim_job(&q, "w1", 10).await;
        assert_eq!(claimed.len(), 1);
        assert_eq!(claimed[0].state, JobState::Running, "claim -> running");
        expire_lease(&pool, id).await; // w1 crashes; the lease lapses.

        let requeued = reap_expired(&pool).await.expect("reap");
        assert_eq!(requeued, 1, "the one expired lease is reaped");

        let job = get_job(&q, id).await;
        assert_eq!(job.state, JobState::Ready, "reaper returns it to ready");
        assert!(job.locked_until.is_none(), "reaper clears the stale lease");

        let reclaimed = claim_job(&q, "w2", 10).await;
        assert_eq!(
            reclaimed.len(),
            1,
            "a second worker reclaims the reaped job"
        );
        assert_eq!(reclaimed[0].id, id);
    }

    /// A lease that is still in the future is left strictly alone: no requeue, the
    /// job stays `running` with an unchanged lease, and nobody else can claim it.
    #[sqlx::test]
    async fn reap_leaves_live_lease_untouched(pool: PgPool) {
        let q = queue(&pool);

        let id = enqueue_job(&q).await;
        let claimed = claim_job(&q, "w1", 10).await;
        let lease = claimed[0].locked_until.expect("claim stamps a lease");

        let requeued = reap_expired(&pool).await.expect("reap");
        assert_eq!(requeued, 0, "a live lease is not reaped");

        let job = get_job(&q, id).await;
        assert_eq!(job.state, JobState::Running, "still held by w1");
        assert_eq!(job.locked_until, Some(lease), "lease is unchanged");

        let other = claim_job(&q, "w2", 10).await;
        assert!(other.is_empty(), "a live-leased job is not claimable");
    }

    /// The reaper's `state = 'running'` guard: a *done* job with a stale
    /// `locked_until` and a never-claimed *ready* job are both skipped — proving it
    /// filters on state, not merely on the clock.
    #[sqlx::test]
    async fn reap_ignores_non_running_jobs_even_with_stale_lease(pool: PgPool) {
        let q = queue(&pool);

        let done_id = enqueue_job(&q).await;
        claim_job(&q, "w1", 10).await;
        q.ack(done_id).await.expect("ack");
        expire_lease(&pool, done_id).await;

        let ready_id = enqueue_job(&q).await;

        let requeued = reap_expired(&pool).await.expect("reap");
        assert_eq!(requeued, 0, "neither a done nor a ready job is reaped");

        let done = get_job(&q, done_id).await;
        assert_eq!(done.state, JobState::Done, "done stays done");

        let ready = get_job(&q, ready_id).await;
        assert_eq!(ready.state, JobState::Ready, "ready stays ready");
    }

    /// `reap_expired` reports how many rows it requeued: three expired leases in a
    /// single sweep return 3.
    #[sqlx::test]
    async fn reap_returns_count_of_all_expired_leases(pool: PgPool) {
        let q = queue(&pool);
        for _ in 0..3 {
            enqueue_job(&q).await;
        }

        let claimed = claim_job(&q, "w1", 10).await;
        assert_eq!(claimed.len(), 3);
        for job in &claimed {
            expire_lease(&pool, job.id).await;
        }

        let requeued = reap_expired(&pool).await.expect("reap");
        assert_eq!(
            requeued, 3,
            "all three expired leases are reaped in one sweep"
        );
    }
}
