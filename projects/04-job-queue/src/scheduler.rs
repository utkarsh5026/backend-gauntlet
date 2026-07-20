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

use chrono::{DateTime, Utc};
use sqlx::PgPool;

use crate::error::AppError;

/// The Postgres `LISTEN`/`NOTIFY` channel name for a given queue.
///
/// Namespacing by queue keeps `NOTIFY`s scoped: a wakeup meant for one queue
/// must never wake a worker parked on another.
fn channel_name(queue: &str) -> String {
    format!("jobs_{queue}")
}

/// Park until either a `NOTIFY` arrives on `queue`'s channel or `poll_fallback`
/// elapses, whichever comes first.
///
/// This is the low-latency alternative to a tight polling loop: an idle worker
/// calls this instead of immediately re-querying the table. Because `NOTIFY` is
/// fire-and-forget (a notification sent while nobody is listening, or dropped
/// on a connection blip, is simply lost), `poll_fallback` guarantees the caller
/// still wakes up and re-checks the durable table on a bounded cadence — the
/// notify only shortens that wait, it never replaces it.
///
/// Returns `Ok(())` in both the "woken by notify" and "poll fallback fired"
/// cases; callers are expected to re-query the queue afterwards rather than
/// branch on which one happened.
///
/// # Errors
///
/// Returns [`AppError`] if establishing the `LISTEN` connection fails, or if
/// the underlying notification stream errors while waiting.
pub async fn wait_for_work(
    pool: &PgPool,
    queue: &str,
    poll_fallback: Duration,
) -> Result<(), AppError> {
    let mut listener = sqlx::postgres::PgListener::connect_with(pool).await?;
    listener.listen(&channel_name(queue)).await?;

    let next_due: Option<DateTime<Utc>> = sqlx::query_scalar!(
        r#"
        SELECT run_at
        FROM jobs
        WHERE queue = $1 AND state = 'ready'
        ORDER BY run_at
        LIMIT 1
        "#,
        queue
    )
    .fetch_optional(pool)
    .await?;

    let sleep_for = next_due
        .map(|at| (at - Utc::now()).to_std().unwrap_or(Duration::ZERO))
        .unwrap_or(poll_fallback)
        .min(poll_fallback);

    tokio::select! {
        notification = listener.recv() => {
            match notification {
                Ok(_notification) => Ok(()),
                Err(e) => Err(e.into()),
            }
        }
        _ = tokio::time::sleep(sleep_for) => Ok(()),
    }
}

/// Send a `NOTIFY` on `queue`'s channel to wake any workers parked in
/// [`wait_for_work`].
///
/// Callers use this whenever they make new work visible sooner than a worker's
/// next poll would find it — e.g. `enqueue`, or a retry/delay coming due. The
/// notify is a payload-less optimization: it carries no job data (workers still
/// go back to the table to find it) and it is fine if nobody is listening.
///
/// # Errors
///
/// Returns [`AppError`] if issuing the `pg_notify` query fails.
pub async fn notify_ready(pool: &PgPool, queue: &str) -> Result<(), AppError> {
    sqlx::query_scalar!(
        r#"SELECT pg_notify($1, '') as "notified!""#,
        channel_name(queue)
    )
    .fetch_one(pool)
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Instant;

    /// A per-test queue name → a per-test NOTIFY channel, so parallel tests can't
    /// wake each other. Combines a nanosecond clock with a process-wide counter.
    fn unique_queue(prefix: &str) -> String {
        static N: AtomicU64 = AtomicU64::new(0);
        let n = N.fetch_add(1, Ordering::Relaxed);
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("{prefix}_{ts}_{n}")
    }

    #[test]
    fn channel_name_is_stable_and_queue_scoped() {
        assert_eq!(channel_name("default"), "jobs_default");
        assert_eq!(channel_name("emails"), "jobs_emails");
        assert_ne!(channel_name("a"), channel_name("b"));
    }

    // `wait_for_work` queries `jobs` for the earliest due `run_at`, so these need a
    // migrated schema. `#[sqlx::test]` gives each case its own DB (CI's shared
    // `DATABASE_URL` is project 01's `shortener` DB and has no `jobs` table).

    #[sqlx::test]
    async fn notify_wakes_an_idle_listener(pool: PgPool) {
        let queue = unique_queue("wake");

        let waiter_pool = pool.clone();
        let waiter_queue = queue.clone();
        let waiter = tokio::spawn(async move {
            let timeout = Duration::from_secs(30);
            wait_for_work(&waiter_pool, &waiter_queue, timeout).await
        });

        tokio::time::sleep(Duration::from_millis(300)).await;
        notify_ready(&pool, &queue)
            .await
            .expect("notify_ready should succeed");

        let joined = tokio::time::timeout(Duration::from_secs(5), waiter)
            .await
            .expect(
                "wait_for_work did not return within 5s — NOTIFY failed to wake it (poll was 30s)",
            );
        joined
            .expect("waiter task panicked")
            .expect("wait_for_work returned Err");
    }

    /// The durable fallback: with **no** NOTIFY at all, `wait_for_work` must still
    /// return after roughly the poll interval. This is what makes NOTIFY a mere
    /// optimisation — a dropped/never-sent notification is never stranded forever.
    #[sqlx::test]
    async fn poll_fallback_returns_without_any_notify(pool: PgPool) {
        let queue = unique_queue("fallback");

        let start = Instant::now();
        wait_for_work(&pool, &queue, Duration::from_millis(300))
            .await
            .expect("wait_for_work should return Ok on the poll fallback");
        let elapsed = start.elapsed();

        assert!(
            elapsed >= Duration::from_millis(250),
            "returned in {elapsed:?} — too early; it should have waited ~300ms for the poll fallback"
        );
        assert!(
            elapsed < Duration::from_secs(3),
            "returned in {elapsed:?} — the poll fallback never fired (idle worker would hang)"
        );
    }

    /// Notifications are scoped per queue: a `NOTIFY` on queue *B* must not wake a
    /// worker listening on queue *A*. If it did, every queue would share one wakeup
    /// and workers would thrash on each other's traffic. Here A's only legitimate
    /// wakeup is its own 500ms poll, so waking earlier than ~400ms means leakage.
    #[sqlx::test]
    async fn notify_is_scoped_to_its_queue_channel(pool: PgPool) {
        let queue_a = unique_queue("scope_a");
        let queue_b = unique_queue("scope_b");

        let waiter_pool = pool.clone();
        let waiter_a = queue_a.clone();
        let waiter = tokio::spawn(async move {
            let start = Instant::now();
            wait_for_work(&waiter_pool, &waiter_a, Duration::from_millis(500))
                .await
                .expect("wait_for_work should return Ok");
            start.elapsed()
        });

        // NOTIFY a *different* queue while A is parked — A must ignore it.
        tokio::time::sleep(Duration::from_millis(150)).await;
        notify_ready(&pool, &queue_b)
            .await
            .expect("notify_ready should succeed");

        let elapsed = tokio::time::timeout(Duration::from_secs(3), waiter)
            .await
            .expect("waiter hung")
            .expect("waiter task panicked");
        assert!(
            elapsed >= Duration::from_millis(400),
            "A woke after only {elapsed:?} — a NOTIFY meant for queue B leaked into queue A"
        );
    }

    #[sqlx::test]
    async fn notify_ready_is_fire_and_forget_without_listeners(pool: PgPool) {
        notify_ready(&pool, &unique_queue("no_listener"))
            .await
            .expect("notify_ready must succeed even with zero listeners");
    }
}
