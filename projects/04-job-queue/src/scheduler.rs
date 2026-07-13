//! V4 — Scheduling + LISTEN/NOTIFY: low pickup latency without busy-polling.
//!
//! Delayed jobs already "work" via V1 (the claim filters `run_at <= now()`), so a
//! polling worker eventually picks them up. The problem V4 solves is the
//! latency-vs-load tradeoff polling forces: poll fast and you flood an idle DB
//! with empty `SELECT`s; poll slow and every job waits.
//!
//! The fix is Postgres `LISTEN`/`NOTIFY`: `enqueue` (and a retry/delay coming
//! due) issues a `NOTIFY` on the queue's channel; idle workers `LISTEN` and wake
//! the instant work appears — with a slow poll as the fallback. `NOTIFY` is
//! fire-and-forget and not durable, so the poll fallback is **not optional**: it
//! keeps the durable table the source of truth and the notify a mere optimization.

use std::time::Duration;

use sqlx::PgPool;

use crate::error::AppError;

fn channel_name(queue: &str) -> String {
    format!("jobs_{queue}")
}

pub async fn wait_for_work(
    pool: &PgPool,
    queue: &str,
    poll_fallback: Duration,
) -> Result<(), AppError> {
    let mut listener = sqlx::postgres::PgListener::connect_with(pool).await?;
    listener.listen(&channel_name(queue)).await?;

    tokio::select! {
        notification = listener.recv() => {
            match notification {
                Ok(_notification) => Ok(()),
                Err(e) => Err(e.into()),
            }
        }
        _ = tokio::time::sleep(poll_fallback) => Ok(()),
    }
}

pub async fn notify_ready(pool: &PgPool, queue: &str) -> Result<(), AppError> {
    sqlx::query_scalar!(
        r#"SELECT pg_notify($1, '') as "notified!""#,
        channel_name(queue)
    )
    .fetch_one(pool)
    .await?;
    Ok(())
}
