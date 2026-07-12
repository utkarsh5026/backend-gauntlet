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
use crate::job::JobId;

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
    Ok(rows.rows_affected())
}

/// Stretch: extend a running job's lease (a heartbeat for long-running jobs so a
/// slow-but-alive worker isn't reaped out from under itself).
pub async fn extend_lease(
    pool: &PgPool,
    job_id: JobId,
    worker_id: &str,
    by: Duration,
) -> Result<(), AppError> {
    let extension = by.as_secs_f64();
    sqlx::query!(
        r#"
        UPDATE jobs SET locked_until = now() + make_interval(secs => $2)
        WHERE id = $1 AND state = 'running' AND locked_by = $3
        "#,
        job_id,
        extension,
        worker_id,
    )
    .execute(pool)
    .await?;
    Ok(())
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
