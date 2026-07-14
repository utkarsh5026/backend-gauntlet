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
