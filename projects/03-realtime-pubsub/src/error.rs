//! A single application error type that turns itself into an HTTP response.
//!
//! Note the split in this project: *HTTP* errors (a rejected `GET /ws` upgrade,
//! a bad auth token) use this type and become status codes. Errors that happen
//! *after* the socket is open are not HTTP anymore — they become a
//! [`ServerMessage::Error`](crate::protocol::ServerMessage) frame or a WebSocket
//! close, handled in the connection loop, not here.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The upgrade request failed auth (missing/invalid token).
    #[error("unauthorized")]
    Unauthorized,

    /// The client sent something we can't act on before the upgrade.
    #[error("bad request: {0}")]
    BadRequest(String),

    /// The cross-node bus (Redis) failed.
    #[error(transparent)]
    Bus(#[from] redis::RedisError),

    /// A directory (admin-panel roster) database query failed.
    #[error(transparent)]
    Db(#[from] sqlx::Error),

    /// A feature is disabled by configuration — e.g. the `/admin` roster API
    /// when `DATABASE_URL` is unset. The pub/sub core runs without it.
    #[error("unavailable: {0}")]
    Unavailable(String),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Unavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
            Self::Db(_) | Self::Bus(_) | Self::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        // Log the full error server-side; only leak a generic message on 5xx so
        // we don't expose internals to clients.
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
