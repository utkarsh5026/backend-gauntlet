//! One application error type that maps cleanly onto gRPC status codes.
//!
//! Engine methods return `Result<T, AppError>` and use `?`; the `From<AppError> for
//! tonic::Status` impl decides what a worker or starter sees. As elsewhere in the
//! gauntlet, we log the full error server-side but hand back only a generic message on
//! internal failures — a durable-execution engine leaks nothing about its innards.

use tonic::Status;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No such execution / run id.
    #[error("not found")]
    NotFound,

    /// The caller sent something we can't act on (bad run id, empty task queue,
    /// a task token that doesn't parse, a command that doesn't belong here…).
    #[error("invalid argument: {0}")]
    InvalidArgument(String),

    /// A worker's replay diverged from the recorded history (V2): the commands it
    /// returned don't match what the engine already knows happened. This is the
    /// non-determinism failure — the workflow code is not a pure function of its
    /// history. It is the worker's bug, not a server fault, so it is NOT a 500.
    #[error("nondeterministic workflow: {0}")]
    NonDeterministic(String),

    /// The durable store (Postgres) failed.
    #[error(transparent)]
    Db(#[from] sqlx::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<AppError> for Status {
    fn from(err: AppError) -> Self {
        match &err {
            AppError::NotFound => Self::not_found("workflow execution not found"),
            AppError::InvalidArgument(msg) => Self::invalid_argument(msg.clone()),
            // failed_precondition: the request was well-formed but the workflow's
            // replay contract was violated — retrying the same task won't help until
            // the workflow code is fixed.
            AppError::NonDeterministic(msg) => Self::failed_precondition(msg.clone()),
            AppError::Db(_) => {
                tracing::error!(error = %err, "workflow store error");
                Self::unavailable("workflow store unavailable")
            }
            AppError::Other(_) => {
                tracing::error!(error = %err, "internal error");
                Self::internal("internal error")
            }
        }
    }
}
