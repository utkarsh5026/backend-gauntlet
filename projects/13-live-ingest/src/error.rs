//! A single application error type that turns itself into an HTTP response.
//!
//! These are the errors of the *delivery* side (a viewer fetching a playlist / init /
//! segment / part). The RTMP ingest side doesn't return HTTP — a publisher session
//! that hits a protocol or auth error just logs and closes the connection (see
//! `session.rs`), it doesn't render a status code.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No stream on air for that key, or the requested msn/part isn't in the live
    /// window (already evicted, or beyond the preload hint).
    #[error("not found")]
    NotFound,

    /// The stream exists but hasn't produced a playable playlist/segment yet
    /// (publisher just connected, first keyframe not in). A player should retry.
    #[error("stream not ready")]
    NotReady,

    /// A malformed request (bad path segment, un-parseable blocking-reload params).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// Building an init/segment/part failed (packaging error).
    #[error("packaging failed: {0}")]
    Packaging(String),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<std::io::Error> for AppError {
    fn from(err: std::io::Error) -> Self {
        AppError::Other(err.into())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            // 503 + Retry-After semantics: the stream is coming up, not broken.
            AppError::NotReady => StatusCode::SERVICE_UNAVAILABLE,
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::Packaging(_) | AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        // Log the full error server-side; only leak a generic message on 5xx so we
        // don't expose internals to clients.
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
