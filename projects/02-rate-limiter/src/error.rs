//! A single application error type that maps cleanly onto gRPC status codes.
//!
//! Handlers return `Result<T, AppError>` and use `?`; the `From<AppError> for
//! tonic::Status` impl decides what the caller sees. As with project 01, we log
//! the full error server-side but only hand a generic message to clients on
//! internal failures.

use tonic::Status;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// Caller sent something we can't act on (empty key, absurd cost, …).
    #[error("invalid argument: {0}")]
    InvalidArgument(String),

    /// The Redis backend that holds shared state failed.
    #[error(transparent)]
    Backend(#[from] redis::RedisError),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<AppError> for Status {
    fn from(err: AppError) -> Self {
        match &err {
            AppError::InvalidArgument(msg) => Self::invalid_argument(msg.clone()),
            AppError::Backend(_) => {
                tracing::error!(error = %err, "rate-limit backend error");
                Self::unavailable("rate limiter backend unavailable")
            }
            AppError::Other(_) => {
                tracing::error!(error = %err, "internal error");
                Self::internal("internal error")
            }
        }
    }
}
