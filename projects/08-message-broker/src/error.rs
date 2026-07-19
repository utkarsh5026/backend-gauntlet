//! A single application error type that turns itself into an HTTP response.
//!
//! The variants track the broker's vocabulary — unknown topic/partition, a
//! record over the size cap, a corrupt frame found on read — so the mapping to
//! status codes is direct.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No topic with that name.
    #[error("unknown topic")]
    UnknownTopic,

    /// Partition index out of range for the topic.
    #[error("unknown partition")]
    UnknownPartition,

    /// Tried to create a topic that already exists.
    #[error("topic already exists")]
    TopicAlreadyExists,

    /// No such consumer group / member (V4).
    #[error("unknown group or member")]
    UnknownGroup,

    /// The request was malformed (bad topic/key name, non-positive partition
    /// count, bad offset, …).
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// A record's value exceeded the configured `MAX_RECORD_BYTES` cap.
    #[error("record too large")]
    RecordTooLarge,

    /// A frame on disk failed its length/CRC check (V1) — corruption or a torn
    /// tail that recovery should have truncated.
    #[error("corrupt log frame")]
    CorruptFrame,

    /// A filesystem operation failed (the log *is* the filesystem).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::UnknownTopic | AppError::UnknownPartition | AppError::UnknownGroup => {
                StatusCode::NOT_FOUND
            }
            AppError::TopicAlreadyExists => StatusCode::CONFLICT,
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::RecordTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::CorruptFrame | AppError::Io(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so
        // we don't expose internals (paths, io details) to clients.
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
