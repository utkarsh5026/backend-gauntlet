//! A single application error type that turns itself into an HTTP response.
//!
//! The variants track the gateway's vocabulary — no route matched, no healthy
//! backend, an upstream that failed or timed out, a request that broke a limit —
//! so the mapping to status codes is direct. A proxy leaks nothing on 5xx.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// No route in the table matched this request (V2) → 404.
    #[error("no route matches this request")]
    NoRoute,

    /// A route matched, but every backend in its pool is unhealthy / open-circuit
    /// (V3/V4) → 503.
    #[error("no healthy backend available")]
    NoHealthyBackend,

    /// The upstream connection/transport failed (refused, reset, DNS) → 502.
    #[error("bad gateway")]
    BadGateway,

    /// The upstream did not respond within the request deadline (V1) → 504.
    #[error("upstream timed out")]
    GatewayTimeout,

    /// The request body exceeded `MAX_BODY_BYTES` (security horizontal) → 413.
    #[error("request body too large")]
    PayloadTooLarge,

    /// The request was malformed (bad header, bad target) → 400.
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            AppError::NoRoute => StatusCode::NOT_FOUND,
            AppError::NoHealthyBackend => StatusCode::SERVICE_UNAVAILABLE,
            AppError::BadGateway => StatusCode::BAD_GATEWAY,
            AppError::GatewayTimeout => StatusCode::GATEWAY_TIMEOUT,
            AppError::PayloadTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        // Log the full error server-side; only the opaque "internal server error"
        // leaks to the client on a 500 so we don't expose internals. The gateway
        // errors (502/503/504) carry a safe, useful message.
        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }
        let client_msg = if status == StatusCode::INTERNAL_SERVER_ERROR {
            "internal server error".to_string()
        } else {
            self.to_string()
        };

        (status, Json(json!({ "error": client_msg }))).into_response()
    }
}
