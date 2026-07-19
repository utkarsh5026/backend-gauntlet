//! The platform error type → HTTP mapping.
//!
//! This capstone has several planes with different failure semantics, but they all
//! surface over HTTP here, so one error enum covers them:
//!
//! - **Control plane** (ingest webhook, session lifecycle): an unknown stream key or a
//!   bad state transition is a `404`/`409`.
//! - **Playback / edge** (`GET …/*.m3u8`, `…/*.m4s`): a miss the origin can't satisfy is a
//!   `404`; an origin timeout is a `502`/`504` — a viewer must get a status, never a hang.
//! - **Chat** (WebSocket): rejections before the upgrade map to a status; after the upgrade,
//!   fan-out failures live on the socket, not here.
//!
//! Handlers return `Result<T, AppError>` and use `?`; the `IntoResponse` impl renders a JSON
//! body. Keep secrets (stream keys, playback tokens) out of the messages — they reach clients.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// Convenience alias so the vertical modules can write `Result<StreamSession>`.
pub type Result<T, E = AppError> = std::result::Result<T, E>;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The stream key / rendition / segment named by the request doesn't exist.
    #[error("not found: {0}")]
    NotFound(String),

    /// A cap was hit (max concurrent streams) or a state transition isn't legal
    /// (e.g. going `Live` from `Offline` without ingest) — the request conflicts
    /// with current state.
    #[error("conflict: {0}")]
    Conflict(String),

    /// The request was rejected before doing work: bad stream key on the ingest
    /// webhook, an invalid/expired playback token, a malformed body.
    #[error("rejected: {0}")]
    Rejected(String),

    /// The edge asked the packager origin for a segment/playlist and it failed or
    /// timed out — a viewer gets a gateway error, not a hung connection.
    #[error("upstream: {0}")]
    Upstream(String),

    /// A dependency (Postgres control plane, Redis chat bus, NATS transcode queue)
    /// was unreachable. Degrade where you can; surface `503` where you can't.
    #[error("dependency unavailable: {0}")]
    Dependency(String),
}

impl AppError {
    /// The HTTP status a handler returns for this error.
    pub fn status(&self) -> StatusCode {
        match self {
            AppError::NotFound(_) => StatusCode::NOT_FOUND,
            AppError::Conflict(_) => StatusCode::CONFLICT,
            AppError::Rejected(_) => StatusCode::BAD_REQUEST,
            AppError::Upstream(_) => StatusCode::BAD_GATEWAY,
            AppError::Dependency(_) => StatusCode::SERVICE_UNAVAILABLE,
        }
    }
}

/// Lets handlers `?` an [`AppError`] straight into a JSON error response.
impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = self.status();
        (status, Json(json!({ "error": self.to_string() }))).into_response()
    }
}
