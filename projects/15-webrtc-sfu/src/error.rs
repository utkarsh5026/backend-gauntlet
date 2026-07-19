//! The SFU error type.
//!
//! There are two planes here with very different failure semantics:
//!
//! - **The media/ICE plane is UDP.** A malformed STUN/RTP/RTCP datagram is *dropped*, not
//!   turned into a status code — an open UDP port takes bytes from anyone, so every parse
//!   is a bounded, non-fatal [`SfuError`] that costs at most one datagram. A length that
//!   doesn't add up, a STUN message with the wrong magic cookie, or a NACK FCI count that
//!   overruns the buffer must all be a recoverable `Err`, never a panic or an OOB index.
//! - **The signaling plane is HTTP.** Those errors (unknown room, room full, bad request)
//!   *do* map to a status code — see [`SfuError::status`], used by the signaling handlers.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// A convenience alias so the vertical modules can write `Result<StunMessage>`.
pub type Result<T, E = SfuError> = std::result::Result<T, E>;

#[derive(Debug, thiserror::Error)]
pub enum SfuError {
    /// A datagram (or a field within it) was shorter than the layout requires.
    #[error("truncated: need at least {need} bytes, got {got}")]
    Truncated { need: usize, got: usize },

    /// A STUN message whose magic cookie wasn't `0x2112A442`, or an RTP packet whose
    /// version bits weren't `2` — i.e. not the protocol we thought it was.
    #[error("bad magic/version: {0}")]
    BadMagic(String),

    /// A well-sized but internally inconsistent datagram (STUN attribute length overruns,
    /// bad FU header, RTCP length word that doesn't fit, NACK FCI count out of range, …).
    #[error("malformed: {0}")]
    Malformed(String),

    /// A STUN MESSAGE-INTEGRITY / FINGERPRINT that didn't verify — the check came from a
    /// peer that doesn't hold the ICE `pwd`. Dropped, never trusted.
    #[error("integrity check failed: {0}")]
    Integrity(String),

    // --- signaling-plane (HTTP) errors ---
    /// A join/publish/subscribe named a room or peer that doesn't exist.
    #[error("not found: {0}")]
    NotFound(String),

    /// A cap was hit (max rooms, max peers) or the request was otherwise invalid.
    #[error("rejected: {0}")]
    Rejected(String),
}

impl SfuError {
    /// The HTTP status a signaling handler returns for this error.
    pub fn status(&self) -> StatusCode {
        match self {
            SfuError::NotFound(_) => StatusCode::NOT_FOUND,
            SfuError::Rejected(_) => StatusCode::CONFLICT,
            // A malformed signaling body lands here.
            _ => StatusCode::BAD_REQUEST,
        }
    }
}

/// Lets signaling handlers `?` an [`SfuError`] straight into a JSON error response.
impl IntoResponse for SfuError {
    fn into_response(self) -> Response {
        let status = self.status();
        (status, Json(json!({ "error": self.to_string() }))).into_response()
    }
}
