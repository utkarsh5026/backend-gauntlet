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
    pub fn backoff(&self, attempt: i32) -> Duration {
        let exponent = (attempt - 1).max(0) as u32;
        let multiplier = 2u32.saturating_pow(exponent);
        let delay = self.base_delay.saturating_mul(multiplier);
        if delay > self.max_delay {
            self.max_delay
        } else {
            let jitter = Duration::from_secs(rand::rng().random_range(0..self.max_delay.as_secs()));
            delay + jitter
        }
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
        Disposition::Retried
    } else {
        Disposition::DeadLettered
    };

    Ok(disposition)
}

#[cfg(test)]
mod tests {
    // TODO(V3): property-test the backoff curve (`proptest` is a dev-dep):
    //   - it never exceeds max_delay;
    //   - it's non-decreasing in `attempt` up to the cap (modulo jitter bounds);
    //   - two calls for the same attempt differ (jitter is actually applied).
}
