//! Security — API-key auth middleware for the write/stats endpoints.
//!
//! Applied as an axum middleware layer in `routes.rs`. Public redirects skip it.

use axum::extract::State;
use axum::http::{header, Request};
use axum::middleware::Next;
use axum::response::Response;

use crate::error::AppError;
use crate::AppState;

/// Reject requests that don't present a valid API key.
///
/// Expected header: `Authorization: Bearer <key>`.
///
/// TODO(security):
/// - Parse the bearer token from the `Authorization` header.
/// - Check it against `state.api_keys` using a **constant-time** comparison
///   (avoid leaking validity via timing — see the `subtle` crate or hash both
///   sides and compare). A plain `HashSet::contains` is the easy version; note
///   in docs/01-design.md why constant-time matters and what you chose.
/// - Never log the key itself.
/// - In a real system keys would be hashed at rest in the DB, not held in memory.
pub async fn require_api_key(
    State(state): State<AppState>,
    req: Request<axum::body::Body>,
    next: Next,
) -> Result<Response, AppError> {
    let _ = &state.api_keys;
    let _present = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .map(str::to_owned);

    // TODO(security): validate `_present` against `state.api_keys` (constant-time).
    // For now, deny everything so you can't forget to implement it.
    let _ = next;
    Err(AppError::Unauthorized)
}
