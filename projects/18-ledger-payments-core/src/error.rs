//! One application error type that renders itself as an HTTP response.
//!
//! The money path has failure modes plain CRUD doesn't: a transfer can be *rejected*
//! (insufficient funds, bad currency), *conflicted* (an idempotency key reused with a
//! different body), or *too contended* (serialization retries exhausted). Each maps to
//! a deliberate status — a rejection is a clean 4xx the client can act on, never a 500.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No such account / transaction.
    #[error("not found")]
    NotFound,

    /// Missing or invalid API key.
    #[error("unauthorized")]
    Unauthorized,

    /// The request failed validation (bad amount, unknown currency, self-transfer…).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// A no-overdraft account can't cover the debit. Rejected, not retryable —
    /// 402 Payment Required is exactly this case.
    #[error("insufficient funds")]
    InsufficientFunds,

    /// An `Idempotency-Key` was reused with a *different* request body (V3). The key
    /// is bound to its first request; a mismatch is a conflict, not a replay.
    #[error("idempotency key conflict: {0}")]
    IdempotencyConflict(String),

    /// Serialization retries were exhausted under contention (V2). The client should
    /// retry — it's a transient conflict, never a 500.
    #[error("too much contention, retry")]
    RetriesExhausted,

    /// A database / transaction operation failed.
    #[error(transparent)]
    Db(#[from] sqlx::Error),

    /// The idempotency cache (Redis) failed. Note: a *cache* failure should usually
    /// degrade to Postgres rather than surface — reserve this for the unrecoverable.
    #[error(transparent)]
    Cache(#[from] redis::RedisError),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::Unauthorized => StatusCode::UNAUTHORIZED,
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::InsufficientFunds => StatusCode::PAYMENT_REQUIRED,
            AppError::IdempotencyConflict(_) => StatusCode::CONFLICT,
            AppError::RetriesExhausted => StatusCode::CONFLICT,
            AppError::Db(_) | AppError::Cache(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so we
        // never expose internals (or a stray secret) to a client.
        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }

        let client_msg = if status.is_server_error() {
            "internal server error".to_string()
        } else {
            self.to_string()
        };

        (status, Json(json!({ "error": client_msg }))).into_response()
    }
}
