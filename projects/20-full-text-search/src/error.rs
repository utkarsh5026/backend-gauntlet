//! A single application error type that turns itself into an HTTP response.
//!
//! The variants track the engine's vocabulary — an unknown document, a query or
//! document that failed validation, a corrupt segment found on read — so the
//! mapping to status codes is direct and no internal detail leaks to a client.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No document with that external id (delete/lookup).
    #[error("not found")]
    NotFound,

    /// Missing or invalid API key on a write/admin route.
    #[error("unauthorized")]
    Unauthorized,

    /// The request was malformed: empty text, a document over the size cap phrased
    /// as validation, a query with too many terms, a bad shard count, …
    #[error("bad request: {0}")]
    BadRequest(String),

    /// A document's text exceeded the configured `MAX_DOC_BYTES` cap.
    #[error("document too large")]
    DocumentTooLarge,

    /// A query had more analyzed terms than `MAX_QUERY_TERMS` allows.
    #[error("query too broad")]
    QueryTooBroad,

    /// A segment on disk failed its integrity/format check (V2) — corruption or a
    /// torn tail a clean flush should never have produced. Never returned as data.
    #[error("corrupt segment")]
    CorruptSegment,

    /// A filesystem operation failed (the index *is* the filesystem).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::Unauthorized => StatusCode::UNAUTHORIZED,
            AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AppError::DocumentTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::QueryTooBroad => StatusCode::BAD_REQUEST,
            AppError::CorruptSegment | AppError::Io(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so we
        // never expose internals (paths, io details) to clients.
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
