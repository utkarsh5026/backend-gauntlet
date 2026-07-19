//! The conferencing error type → HTTP mapping.
//!
//! Project 17 has three planes with different failure semantics, all surfacing over HTTP here,
//! so one error enum covers them:
//!
//! - **Signaling** (publish/subscribe): an unknown publisher/room is a `404`; a full room or a
//!   room the local SFU can't place (no quorum) is a `409`/`503`; a malformed body is a `400`.
//! - **Consensus / placement** (V1, `/cluster/*` + placement): not-leader forwards or refuses,
//!   a minority partition can't place a new room — `409`/`503`, never a silent split.
//! - **Cascade** (V2): a relay leg that can't be opened, or a peer region that's unreachable, is
//!   a dependency failure (`502`/`503`) — degrade the far region, don't hang the near one.
//!
//! Handlers return `Result<T, AppError>` and use `?`; the `IntoResponse` impl renders a JSON
//! body. Keep secrets (ICE creds, cluster secret) out of the messages — they reach clients.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// Convenience alias so the vertical modules can write `Result<RoomPlacement>`.
pub type Result<T, E = AppError> = std::result::Result<T, E>;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// The room / publisher / recording named by the request doesn't exist.
    #[error("not found: {0}")]
    NotFound(String),

    /// A cap was hit (max rooms/peers/relay legs) or a state transition isn't legal
    /// (e.g. placing a room that already has a home) — the request conflicts with state.
    #[error("conflict: {0}")]
    Conflict(String),

    /// The request was rejected before doing work: a malformed publish body, a bad
    /// cluster secret on an inter-SFU RPC, an unauthenticated relay packet.
    #[error("rejected: {0}")]
    Rejected(String),

    /// This node isn't the placement leader and couldn't act. Carries where to retry
    /// (the current leader, if known) so signaling can forward instead of failing.
    #[error("not leader{}", .0.as_ref().map(|l| format!(" (leader: {l})")).unwrap_or_default())]
    NotLeader(Option<String>),

    /// A relay leg / peer SFU / packager the cascade reached out to failed or timed out —
    /// the far region degrades, the near one keeps serving.
    #[error("upstream: {0}")]
    Upstream(String),

    /// No quorum reachable (minority partition), or a required peer/dependency was
    /// unreachable. A new room can't be placed here; already-committed rooms still serve.
    #[error("unavailable: {0}")]
    Unavailable(String),
}

impl AppError {
    /// The HTTP status a handler returns for this error.
    pub fn status(&self) -> StatusCode {
        match self {
            AppError::NotFound(_) => StatusCode::NOT_FOUND,
            AppError::Conflict(_) => StatusCode::CONFLICT,
            AppError::Rejected(_) => StatusCode::BAD_REQUEST,
            // A not-leader is retryable elsewhere; 421 Misdirected Request says "not me".
            AppError::NotLeader(_) => StatusCode::MISDIRECTED_REQUEST,
            AppError::Upstream(_) => StatusCode::BAD_GATEWAY,
            AppError::Unavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
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
