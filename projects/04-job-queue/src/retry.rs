//! V3 — Retries with backoff + the dead-letter queue.
//!
//! Jobs fail. The policy here is what keeps one bad job from taking the system
//! down: on failure, retry with an **exponentially backed-off, jittered** delay
//! up to `max_attempts`, then move the job to the **dead-letter queue** instead
//! of looping forever. A *poison message* (one that fails every time) is exactly
//! the case the DLQ exists for.

use std::time::Duration;

use chrono::{Duration as ChronoDuration, Utc};
use rand::Rng;
use sqlx::PgPool;

use crate::error::AppError;
use crate::job::{Job, JobState};

const BASE_DELAY: Duration = Duration::from_secs(1);
const MAX_DELAY: Duration = Duration::from_secs(300);

/// Backoff parameters for the retry schedule.
#[derive(Debug, Clone, Copy)]
pub struct RetryPolicy {
    /// Base unit of delay; the first retry waits roughly this long.
    pub base_delay: Duration,
    /// Cap so the exponential curve can't schedule a job years out.
    pub max_delay: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            base_delay: BASE_DELAY,
            max_delay: MAX_DELAY,
        }
    }
}

impl RetryPolicy {
    /// The exponential **ceiling** for a given attempt: `base · 2^(attempt-1)`,
    /// saturating so a large attempt can't overflow, then capped at `max_delay`.
    /// This is the *upper bound* of the wait — the actual delay is a jittered draw
    /// within it (see [`RetryPolicy::backoff`]). Keeping it separate makes the
    /// growth curve deterministic and testable; the randomness lives only in
    /// `backoff`.
    fn ceiling(&self, attempt: i32) -> Duration {
        let exponent = (attempt - 1).max(0) as u32;
        let multiplier = 2u32.saturating_pow(exponent);
        self.base_delay
            .saturating_mul(multiplier)
            .min(self.max_delay)
    }

    /// **Full jitter** backoff (AWS's "Exponential Backoff And Jitter"): wait a
    /// uniformly random duration in `[0, ceiling(attempt)]`.
    ///
    /// The jitter is proportional to the *current* exponential ceiling, not to
    /// `max_delay` — so attempt 1 waits at most `base_delay`, while later attempts
    /// spread across the full cap. Because the ceiling is capped *before* we draw,
    /// the result can never exceed `max_delay`: no post-hoc clamp is needed.
    /// Allowing a draw of zero is deliberate — it maximally de-synchronises a herd
    /// of workers that all failed at once, instead of re-colliding them at `2^n`.
    pub fn backoff(&self, attempt: i32) -> Duration {
        let ceiling = self.ceiling(attempt).as_millis() as u64;
        Duration::from_millis(rand::rng().random_range(0..=ceiling))
    }
}

/// What happened to a job that failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Rescheduled for another attempt after a backoff delay.
    Retried,
    /// Out of attempts — moved to the dead-letter queue.
    DeadLettered,
}

/// Handle a job that failed: bump the attempt, record the error, and either
/// reschedule it with backoff or dead-letter it when its attempts are spent.
pub async fn nack(
    pool: &PgPool,
    policy: &RetryPolicy,
    job: &Job,
    error: &str,
) -> Result<Disposition, AppError> {
    let attempts = job.attempts + 1;
    let new_error = error.to_string();
    let backoff = policy.backoff(attempts);
    let new_run_at =
        Utc::now() + ChronoDuration::from_std(backoff).unwrap_or_else(|_| ChronoDuration::zero());
    let job_state = if attempts < job.max_attempts {
        JobState::Ready
    } else {
        JobState::Dead
    };

    sqlx::query!(
        r#"
        UPDATE jobs
        SET
            attempts = $2,
            last_error = $3,
            state = $4,
            run_at = $5,
            locked_by = NULL,
            locked_at = NULL,
            locked_until = NULL
        WHERE id = $1
        "#,
        job.id,
        attempts,
        new_error,
        job_state.as_str(),
        new_run_at,
    )
    .execute(pool)
    .await?;

    let disposition = if job_state == JobState::Ready {
        metrics::counter!(crate::metrics::RETRIED_TOTAL, "kind" => job.kind.clone()).increment(1);
        Disposition::Retried
    } else {
        metrics::counter!(crate::metrics::DEAD_LETTERED_TOTAL, "kind" => job.kind.clone())
            .increment(1);
        Disposition::DeadLettered
    };
    Ok(disposition)
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    use serde_json::json;

    use crate::job::{JobId, NewJob};
    use crate::queue::Queue;

    const QUEUE_NAME: &str = "emails";
    const DEFAULT_MAX_ATTEMPTS: i32 = 5;
    const LEASE: Duration = Duration::from_secs(30);

    fn queue(pool: &PgPool) -> Arc<Queue> {
        Queue::new(pool.clone(), DEFAULT_MAX_ATTEMPTS)
    }

    fn new_job(max_attempts: Option<i32>) -> NewJob {
        NewJob {
            queue: QUEUE_NAME.to_string(),
            kind: "send_email".into(),
            payload: json!({ "to": "a@b.com" }),
            max_attempts,
            delay_secs: None,
        }
    }

    async fn enqueue_job(q: &Queue, max_attempts: Option<i32>) -> JobId {
        q.enqueue(new_job(max_attempts)).await.expect("enqueue")
    }

    /// Claim the single ready job — the running row `nack` will act on.
    async fn claim_one(q: &Queue) -> Job {
        let mut batch = q.claim(QUEUE_NAME, "w1", 10, LEASE).await.expect("claim");
        assert_eq!(batch.len(), 1, "expected exactly one claimable job");
        batch.pop().unwrap()
    }

    async fn get_job(q: &Queue, id: JobId) -> Job {
        q.get(id).await.expect("get").expect("job exists")
    }

    /// Fast-forward a rescheduled job so its backoff has "elapsed" and it's due
    /// now — lets a retry loop run without sleeping through real backoff delays.
    /// Unchecked `query` keeps this test-only statement out of the sqlx cache.
    async fn make_due_now(pool: &PgPool, id: JobId) {
        sqlx::query("UPDATE jobs SET run_at = now() WHERE id = $1")
            .bind(id)
            .execute(pool)
            .await
            .expect("make job due now");
    }

    // ---- backoff curve (pure) -------------------------------------------------

    /// The exponential ceiling grows `base · 2^(attempt-1)` and then pins at
    /// `max_delay`: attempt 1 is exactly the base, and a large attempt saturates
    /// to the cap instead of overflowing.
    #[test]
    fn ceiling_grows_then_caps_at_max_delay() {
        let p = RetryPolicy::default();
        assert_eq!(p.ceiling(1), p.base_delay, "attempt 1 ceiling is the base");
        assert_eq!(p.ceiling(64), p.max_delay);
        assert_eq!(p.ceiling(1000), p.max_delay);
    }

    /// The curve is **non-decreasing** up to the cap — each attempt's ceiling is at
    /// least the previous one's. This is precisely the property a jitter scaled to
    /// `max_delay` (rather than to the current ceiling) silently violated: it made
    /// every attempt statistically identical.
    #[test]
    fn ceiling_is_non_decreasing_up_to_the_cap() {
        let p = RetryPolicy::default();
        for attempt in 1..20 {
            assert!(
                p.ceiling(attempt + 1) >= p.ceiling(attempt),
                "ceiling must not shrink from attempt {attempt} to {}",
                attempt + 1,
            );
        }
    }

    /// Full jitter draws within `[0, ceiling(attempt)]`: every sample sits at or
    /// below that attempt's ceiling, sampled across attempts and many draws.
    #[test]
    fn backoff_stays_within_the_ceiling() {
        let p = RetryPolicy::default();
        for attempt in 1..=20 {
            let ceiling = p.ceiling(attempt);
            for _ in 0..50 {
                assert!(
                    p.backoff(attempt) <= ceiling,
                    "backoff({attempt}) exceeded its ceiling {ceiling:?}",
                );
            }
        }
    }

    /// Jitter is actually applied: a fixed (jitter-free) schedule would return an
    /// identical value every time, so repeated draws for the *same* attempt must
    /// not all be equal — that spread is what breaks the thundering herd.
    #[test]
    fn backoff_applies_jitter() {
        let p = RetryPolicy::default();
        let first = p.backoff(1);
        let varied = (0..64).any(|_| p.backoff(1) != first);
        assert!(
            varied,
            "jitter should make repeated backoffs for the same attempt differ",
        );
    }

    /// The SPEC's "capped at a maximum" box: no matter the attempt or the jitter
    /// draw, the backoff never exceeds `max_delay`. Sampled across attempts and
    /// many jitter draws so an off-by-one in the final clamp can't slip through.
    #[test]
    fn backoff_never_exceeds_max_delay() {
        let p = RetryPolicy::default();
        for attempt in 1..=20 {
            for _ in 0..50 {
                assert!(
                    p.backoff(attempt) <= p.max_delay,
                    "backoff({attempt}) exceeded the cap {:?}",
                    p.max_delay,
                );
            }
        }
    }

    // ---- nack lifecycle (Postgres) --------------------------------------------

    /// A failure with attempts to spare is rescheduled, not dropped: the attempt
    /// is counted, the error recorded, the lease cleared, and `run_at` pushed into
    /// the future by the backoff — the job goes back to `ready`.
    #[sqlx::test]
    async fn nack_reschedules_with_remaining_attempts(pool: PgPool) {
        let q = queue(&pool);
        let id = enqueue_job(&q, None).await; // default max_attempts = 5
        let job = claim_one(&q).await;
        assert_eq!(job.attempts, 0, "no attempts have run yet");

        let before = Utc::now();
        let disp = nack(&pool, &RetryPolicy::default(), &job, "boom")
            .await
            .expect("nack");
        assert_eq!(disp, Disposition::Retried, "attempts remain -> retried");

        let after = get_job(&q, id).await;
        assert_eq!(after.state, JobState::Ready, "rescheduled back to ready");
        assert_eq!(after.attempts, 1, "the failed attempt is counted");
        assert_eq!(after.last_error.as_deref(), Some("boom"), "error recorded");
        assert!(
            after.locked_until.is_none(),
            "the lease is cleared on failure"
        );
        // Full jitter reschedules within `[now, now + ceiling(1)]`, and here
        // `ceiling(1)` is just the base delay. A draw of 0 is legitimate, so bound
        // both sides rather than asserting a strict push-out into the future.
        assert!(
            after.run_at >= before,
            "run_at is not scheduled in the past"
        );
        assert!(
            after.run_at <= before + ChronoDuration::seconds(5),
            "run_at stays within the small first-attempt ceiling",
        );
    }

    /// A failure on the last attempt dead-letters instead of rescheduling: the job
    /// lands in the DLQ (`dead`) and is terminal — never claimable again.
    #[sqlx::test]
    async fn nack_dead_letters_when_attempts_exhausted(pool: PgPool) {
        let q = queue(&pool);
        let id = enqueue_job(&q, Some(1)).await; // one and done
        let job = claim_one(&q).await;

        let disp = nack(&pool, &RetryPolicy::default(), &job, "poison")
            .await
            .expect("nack");
        assert_eq!(disp, Disposition::DeadLettered, "no attempts left -> DLQ");

        let dead = get_job(&q, id).await;
        assert_eq!(
            dead.state,
            JobState::Dead,
            "landed in the dead-letter queue"
        );
        assert_eq!(dead.attempts, 1, "the final attempt is counted");
        assert_eq!(dead.last_error.as_deref(), Some("poison"), "error recorded");

        // A dead job is terminal — never handed out again.
        let again = q.claim(QUEUE_NAME, "w2", 10, LEASE).await.expect("claim");
        assert!(again.is_empty(), "a dead job is not claimable");
    }

    /// The headline V3 Proof: a poison message that fails *every* time retries up
    /// to the cap and then stops in the DLQ — it does not loop forever. The bounded
    /// outer loop is the safety net: if the DLQ never caught it, this would spin
    /// past `MAX` and the disposition sequence assertion would fail.
    #[sqlx::test]
    async fn nack_poison_message_reaches_dlq_and_stops(pool: PgPool) {
        let q = queue(&pool);
        const MAX: i32 = 3;
        let id = enqueue_job(&q, Some(MAX)).await;
        let policy = RetryPolicy::default();

        let mut dispositions = Vec::new();
        for _ in 0..MAX + 5 {
            let job = claim_one(&q).await;
            let disp = nack(&pool, &policy, &job, "always fails")
                .await
                .expect("nack");
            dispositions.push(disp);
            if disp == Disposition::DeadLettered {
                break;
            }
            make_due_now(&pool, id).await; // skip the backoff so we can retry now
        }

        assert_eq!(
            dispositions,
            vec![
                Disposition::Retried,
                Disposition::Retried,
                Disposition::DeadLettered,
            ],
            "a poison message retries up to the cap, then dead-letters exactly once",
        );
        assert_eq!(
            get_job(&q, id).await.state,
            JobState::Dead,
            "it stops in the DLQ; it does not loop forever",
        );
    }
}
