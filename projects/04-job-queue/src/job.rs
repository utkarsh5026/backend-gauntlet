//! Shared job types: the row shape, its lifecycle state, and the enqueue input.
//!
//! These are the values the verticals pass around — `enqueue` takes a [`NewJob`],
//! `claim` hands a worker a [`Job`], and the worker drives it through [`JobState`].

use std::str::FromStr;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::postgres::{PgTypeInfo, PgValueRef};
use sqlx::{Decode, Postgres, Type};

use crate::error::AppError;

/// Database identity of a job (`jobs.id`, a `BIGSERIAL`).
pub type JobId = i64;

/// Where a job is in its lifecycle. Persisted as the `jobs.state` text column.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Ready,
    Running,
    Done,
    Dead,
}

impl JobState {
    pub fn as_str(self) -> &'static str {
        match self {
            JobState::Ready => "ready",
            JobState::Running => "running",
            JobState::Done => "done",
            JobState::Dead => "dead",
        }
    }
}

impl FromStr for JobState {
    type Err = AppError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "ready" => JobState::Ready,
            "running" => JobState::Running,
            "done" => JobState::Done,
            "dead" => JobState::Dead,
            other => {
                return Err(AppError::Other(anyhow::anyhow!(
                    "unknown job state: {other}"
                )));
            }
        })
    }
}

impl Type<Postgres> for JobState {
    fn type_info() -> PgTypeInfo {
        <String as Type<Postgres>>::type_info()
    }
}

impl<'r> Decode<'r, Postgres> for JobState {
    fn decode(value: PgValueRef<'r>) -> Result<Self, sqlx::error::BoxDynError> {
        let s = <&str as Decode<Postgres>>::decode(value)?;
        s.parse()
            .map_err(|err| -> sqlx::error::BoxDynError { Box::new(err) })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Job {
    pub id: JobId,
    pub queue: String,
    pub kind: String,
    pub payload: serde_json::Value,
    pub state: JobState,
    pub attempts: i32,
    pub max_attempts: i32,
    pub run_at: DateTime<Utc>,
    pub locked_until: Option<DateTime<Utc>>,
    pub last_error: Option<String>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NewJob {
    pub queue: String,
    pub kind: String,
    #[serde(default)]
    pub payload: serde_json::Value,
    /// Optional override; falls back to the server default when `None`.
    #[serde(default)]
    pub max_attempts: Option<i32>,
    /// Delay before the job becomes eligible, in seconds. `None`/`0` = run now (V4).
    #[serde(default)]
    pub delay_secs: Option<i64>,
}

/// Max serialized bytes of a job `payload` (64 KiB). This is the *semantic* cap on
/// the payload specifically; the request-body limit in `routes.rs` is a coarser
/// outer guard. A payload is a *reference to work* (an id / object key), not the
/// work itself — and the row it lands in is re-read on every claim, retry, and GET.
const MAX_PAYLOAD_BYTES: usize = 64 * 1024;
/// Max length, in bytes, of the `queue` / `kind` identifiers.
const MAX_NAME_LEN: usize = 64;
/// Inclusive ceiling on a caller-supplied `max_attempts` — stops a caller turning a
/// poison job into a million-retry slow loop that never reaches the DLQ.
const MAX_ATTEMPTS_CEILING: i32 = 25;
/// Ceiling on `delay_secs` (30 days) — bounds how far a job may be scheduled out.
const MAX_DELAY_SECS: i64 = 30 * 24 * 60 * 60;

impl NewJob {
    /// Validate + bound everything the caller controls, before it becomes a row.
    /// Returns a human-readable reason on the first failure, which the enqueue
    /// handler maps to [`AppError::BadRequest`] → 400. Pure (no I/O), so it's
    /// unit-tested directly.
    pub fn validate(&self) -> Result<(), String> {
        validate_name("queue", &self.queue)?;
        validate_name("kind", &self.kind)?;

        let payload_bytes = serde_json::to_vec(&self.payload)
            .map(|v| v.len())
            .unwrap_or(0);
        if payload_bytes > MAX_PAYLOAD_BYTES {
            return Err(format!(
                "payload too large: {payload_bytes} bytes (max {MAX_PAYLOAD_BYTES})"
            ));
        }

        if let Some(max_attempts) = self.max_attempts {
            if !(1..=MAX_ATTEMPTS_CEILING).contains(&max_attempts) {
                return Err(format!(
                    "max_attempts must be in 1..={MAX_ATTEMPTS_CEILING}, got {max_attempts}"
                ));
            }
        }

        if let Some(delay) = self.delay_secs {
            if delay > MAX_DELAY_SECS {
                return Err(format!("delay_secs too large: {delay} (max {MAX_DELAY_SECS})"));
            }
        }

        Ok(())
    }
}

/// A `queue`/`kind` identifier must be non-empty, within [`MAX_NAME_LEN`], and made
/// only of ASCII alphanumerics, `_`, or `-`. These names become a `NOTIFY` channel
/// (`jobs_{queue}`) and a metric label, so arbitrary or huge values would blow up
/// label cardinality and produce surprising channel names.
fn validate_name(field: &str, value: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{field} must not be empty"));
    }
    if value.len() > MAX_NAME_LEN {
        return Err(format!(
            "{field} too long: {} bytes (max {MAX_NAME_LEN})",
            value.len()
        ));
    }
    if !value
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err(format!(
            "{field} has invalid characters (allowed: ASCII alphanumeric, '_', '-')"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use serde_json::json;

    /// A valid baseline `NewJob` the negative cases mutate one field at a time.
    fn valid() -> NewJob {
        NewJob {
            queue: "emails".into(),
            kind: "send_email".into(),
            payload: json!({ "to": "a@b.com" }),
            max_attempts: Some(5),
            delay_secs: Some(60),
        }
    }

    #[test]
    fn accepts_a_well_formed_job() {
        assert!(valid().validate().is_ok());
    }

    #[test]
    fn accepts_boundary_values() {
        let mut j = valid();
        j.max_attempts = Some(MAX_ATTEMPTS_CEILING); // top of the range
        j.delay_secs = Some(MAX_DELAY_SECS); // exactly the cap
        j.queue = "a".repeat(MAX_NAME_LEN); // exactly the max length
        assert!(j.validate().is_ok(), "boundary values are inclusive");
    }

    #[test]
    fn rejects_empty_or_overlong_names() {
        let mut j = valid();
        j.queue = String::new();
        assert!(j.validate().is_err(), "empty queue");

        let mut j = valid();
        j.kind = "k".repeat(MAX_NAME_LEN + 1);
        assert!(j.validate().is_err(), "overlong kind");
    }

    #[test]
    fn rejects_bad_charset_in_names() {
        for bad in ["my queue", "emails!", "a/b", "drop;table", "café"] {
            let mut j = valid();
            j.queue = bad.into();
            assert!(j.validate().is_err(), "queue {bad:?} should be rejected");
        }
    }

    #[test]
    fn rejects_oversized_payload() {
        let mut j = valid();
        // A JSON string whose bytes exceed the cap.
        j.payload = json!({ "blob": "x".repeat(MAX_PAYLOAD_BYTES + 1) });
        assert!(j.validate().is_err(), "payload over the cap is rejected");
    }

    #[test]
    fn rejects_out_of_range_max_attempts() {
        for bad in [0, -1, MAX_ATTEMPTS_CEILING + 1] {
            let mut j = valid();
            j.max_attempts = Some(bad);
            assert!(j.validate().is_err(), "max_attempts {bad} should be rejected");
        }
    }

    #[test]
    fn rejects_delay_over_the_ceiling() {
        let mut j = valid();
        j.delay_secs = Some(MAX_DELAY_SECS + 1);
        assert!(j.validate().is_err());
    }
}
