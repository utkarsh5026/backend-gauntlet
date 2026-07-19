//! A single application error type that turns itself into an HTTP response.
//!
//! These are the errors of the *ingest + query API*. The consumer pipeline
//! (rollup → sink) doesn't return HTTP — a point that fails to parse is rejected
//! and counted (see `parse.rs`), and a sink write that fails is retried by
//! redelivery (at-least-once, see `sink.rs`), handled in the pipeline loop.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The request body / query failed validation (malformed line, bad range,
    /// oversized payload, too many tags, …).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// Nothing matched (e.g. a query range with no rollups).
    #[error("not found")]
    NotFound,

    /// Publishing to the durable stream failed (broker unavailable, etc.).
    #[error("broker error: {0}")]
    Broker(String),

    /// A ClickHouse read/write failed.
    #[error("store error: {0}")]
    Store(String),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::Broker(_) | AppError::Store(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so
        // we don't expose internals (or the broker/store topology) to clients.
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
