//! A single application error type that turns itself into an HTTP response.
//!
//! These are the errors of the *control-plane API* (submit a job, inspect it).
//! The worker/data-plane side doesn't return HTTP — a task that fails becomes a
//! retry or a dead task (see `worker.rs` + the retry/lease logic), settled inside
//! the worker loop, not here.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No job/task with that id.
    #[error("not found")]
    NotFound,

    /// The request body failed validation (bad source path, empty ladder, …).
    #[error("bad request: {0}")]
    BadRequest(String),

    /// An `ffmpeg` / `ffprobe` invocation failed (spawn error or non-zero exit).
    /// Carries the tool's stderr so the failure is diagnosable in logs.
    #[error("transcode tool failed: {0}")]
    Ffmpeg(String),

    /// A database / DAG-store operation failed.
    #[error(transparent)]
    Db(#[from] sqlx::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::Ffmpeg(_) | AppError::Db(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so
        // we don't expose internals (paths, stderr) to clients.
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
