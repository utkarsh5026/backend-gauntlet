//! A single application error type that turns itself into an HTTP response.
//!
//! The variants mirror the S3 error vocabulary (`NoSuchKey`, `NoSuchBucket`, …)
//! so the mapping to status codes is direct. Responses are the S3 XML
//! `<Error><Code>…</Code><Message>…</Message></Error>` envelope.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};

use crate::s3_xml::{error_body, error_code, xml_response};

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No bucket with that name.
    #[error("no such bucket")]
    NoSuchBucket,

    /// No object with that key in the bucket.
    #[error("no such key")]
    NoSuchKey,

    /// No in-progress multipart upload with that id (V4).
    #[error("no such upload")]
    NoSuchUpload,

    /// Tried to create a bucket that already exists.
    #[error("bucket already exists")]
    BucketAlreadyExists,

    /// The request was malformed (bad bucket/key name, bad multipart args, …).
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// An object or part exceeded the configured `MAX_OBJECT_SIZE` cap (V2).
    #[error("entity too large")]
    EntityTooLarge,

    /// Conditional request failed (`If-Match` / `If-None-Match` write guard).
    ///
    /// Maps to HTTP 412 — the classic S3 response when a compare-and-swap
    /// precondition does not hold (stale ETag, or the key is absent).
    #[error("precondition failed")]
    PreconditionFailed,

    /// A filesystem operation failed (the store *is* the filesystem).
    #[error(transparent)]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<serde_json::Error> for AppError {
    fn from(err: serde_json::Error) -> Self {
        Self::Other(err.into())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::NoSuchBucket | Self::NoSuchKey | Self::NoSuchUpload => StatusCode::NOT_FOUND,
            Self::BucketAlreadyExists => StatusCode::CONFLICT,
            Self::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            Self::EntityTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            Self::PreconditionFailed => StatusCode::PRECONDITION_FAILED,
            Self::Io(_) | Self::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }

        let client_msg = if status.is_server_error() {
            "internal server error".to_string()
        } else {
            self.to_string()
        };

        let code = error_code(&self);
        xml_response(status, error_body(code, &client_msg))
    }
}
