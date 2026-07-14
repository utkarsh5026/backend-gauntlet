//! A single application error type that turns itself into an HTTP response.
//!
//! These are the errors of the *producer/admin API* (enqueue, status, DLQ). The
//! worker side doesn't return HTTP — a job that fails becomes a retry or a
//! dead-letter (see `retry.rs`), handled in the worker loop, not here.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No job with that id.
    #[error("not found")]
    NotFound,

    /// Missing or invalid enqueue credential (no/ wrong `Authorization: Bearer` token).
    #[error("unauthorized")]
    Unauthorized,

    /// The request body failed validation (bad/oversized payload, etc.).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// A database / queue operation failed.
    #[error(transparent)]
    Db(#[from] sqlx::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Db(_) | Self::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

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
