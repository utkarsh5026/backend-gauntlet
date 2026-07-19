//! A single application error type that turns itself into an HTTP response.
//!
//! The variants track the packager's vocabulary — an unknown asset/rendition, a
//! segment index out of range, a source container we couldn't parse, a bad
//! `Range` header — so the mapping to status codes is direct.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No asset with that name in the library.
    #[error("unknown asset")]
    UnknownAsset,

    /// The asset exists, but not at that rendition (bitrate ladder rung).
    #[error("unknown rendition")]
    UnknownRendition,

    /// A segment index past the end of the segmented rendition (V2/V4).
    #[error("segment out of range")]
    SegmentOutOfRange,

    /// The request was malformed — a bad `Range` header, a non-numeric segment
    /// index, an asset/rendition name that fails validation.
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// A `Range` request that can't be satisfied (start past EOF) → `416` with a
    /// `Content-Range: bytes */<len>` (V4).
    #[error("range not satisfiable")]
    RangeNotSatisfiable,

    /// A source file that isn't a container we can demux (V1) — the file is the
    /// server's own, so this is a 5xx, not a client error.
    #[error("malformed source media: {0}")]
    MalformedMedia(String),

    /// A filesystem operation failed (the library *is* the filesystem).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::UnknownAsset | AppError::UnknownRendition | AppError::SegmentOutOfRange => {
                StatusCode::NOT_FOUND
            }
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::RangeNotSatisfiable => StatusCode::RANGE_NOT_SATISFIABLE,
            AppError::MalformedMedia(_) | AppError::Io(_) | AppError::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        // Log the full error server-side; only leak a generic message on 5xx so we
        // don't expose internals (paths, parser details) to clients.
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
