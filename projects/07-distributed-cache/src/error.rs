//! A single application error type that turns itself into an HTTP response.
//!
//! Handlers return `Result<T, AppError>` and use `?`; this maps each variant to a
//! status code and a JSON body, logging the full error only on 5xx so we never
//! leak internals (peer addresses, io detail) to a client.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The key is not present (or expired) on any replica.
    #[error("not found")]
    NotFound,

    /// The request was malformed (bad key charset/length, oversized value, …).
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// The value exceeds the configured per-entry size cap.
    #[error("value too large")]
    ValueTooLarge,

    /// A write/admin request lacked a valid auth token (security horizontal).
    #[error("unauthorized")]
    Unauthorized,

    /// No live node currently owns this key (cluster still converging, or all of
    /// the key's replicas are down). Distinct from `NotFound`: the key *might*
    /// exist, we just can't reach an owner right now.
    #[error("no owner available for key")]
    Unavailable,

    /// Forwarding a request to a peer replica failed (network, timeout, 5xx).
    #[error("peer request failed: {0}")]
    Upstream(String),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NotFound => StatusCode::NOT_FOUND,
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::ValueTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::Unauthorized => StatusCode::UNAUTHORIZED,
            // The cluster can't serve this right now, but the client may retry.
            AppError::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
            AppError::Upstream(_) => StatusCode::BAD_GATEWAY,
            AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
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
