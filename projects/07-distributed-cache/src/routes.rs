//! HTTP surface: the public client API + the internal node-to-node RPC.
//!
//! The routing and body handling are wired. The public `/cache` handlers call the
//! coordinator (V4), which decides local-vs-forward; the `/internal/cache`
//! handlers hit the *local* store only (they're the endpoint a peer coordinator
//! forwards to). Run as-is and `GET /healthz` + `GET /cluster` work; the first
//! real cache op panics on a V1/V2/V4 `todo!()`, which is the worklist.
//!
//! Public vs internal are deliberately separate paths (SPEC: internal RPC must not
//! be spoofable as a client request) — TODO(security): authenticate both writes
//! and the whole `/internal` tree.

use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::Deserialize;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::AppState;

/// Max key length; a key also can't be empty or contain a path separator (it goes
/// in a URL and, forwarded, another URL). A cheap guard, not the whole security story.
const MAX_KEY_LEN: usize = 512;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/cluster", get(cluster))
        // Public client API: coordinator decides local vs forward.
        .route(
            "/cache/{key}",
            get(get_cache).put(put_cache).delete(delete_cache),
        )
        // Internal node-to-node RPC: operates on THIS node's local store only.
        // TODO(security): gate this whole subtree behind the cluster auth token so
        // an outsider can't inject values by pretending to be a peer.
        .route(
            "/internal/cache/{key}",
            get(internal_get).put(internal_put).delete(internal_delete),
        )
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /cluster` — this node's membership view (observability + convergence tests).
async fn cluster(State(state): State<AppState>) -> Response {
    Json(state.membership.snapshot()).into_response()
}

/// Validate a key from the path before it touches the ring or the store.
fn check_key(key: &str) -> Result<(), AppError> {
    if key.is_empty() || key.len() > MAX_KEY_LEN {
        return Err(AppError::InvalidRequest(format!(
            "key must be 1..={MAX_KEY_LEN} bytes"
        )));
    }
    Ok(())
}

/// `GET /cache/{key}` — resolve via the coordinator (local or forwarded).
async fn get_cache(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Response, AppError> {
    check_key(&key)?;
    match state.coordinator.get(&key).await? {
        Some(bytes) => Ok((StatusCode::OK, bytes).into_response()),
        None => Err(AppError::NotFound),
    }
}

/// `PUT /cache/{key}` — store, replicated to the key's owners. Optional `?ttl=<secs>`.
///
/// TODO(security): require the cluster auth token here before writing.
async fn put_cache(
    State(state): State<AppState>,
    Path(key): Path<String>,
    Query(q): Query<TtlQuery>,
    body: Bytes,
) -> Result<StatusCode, AppError> {
    check_key(&key)?;
    if body.len() as u64 > state.max_value_bytes {
        return Err(AppError::ValueTooLarge);
    }
    let ttl = q.ttl.map(Duration::from_secs);
    state.coordinator.put(key, body, ttl).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// `DELETE /cache/{key}` — evict from all replicas.
async fn delete_cache(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<StatusCode, AppError> {
    check_key(&key)?;
    state.coordinator.delete(&key).await?;
    Ok(StatusCode::NO_CONTENT)
}

// --- internal RPC: local store only (no routing) ------------------------------

async fn internal_get(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Response, AppError> {
    match state.coordinator.local_get(&key) {
        Some(bytes) => Ok((StatusCode::OK, bytes).into_response()),
        None => Err(AppError::NotFound),
    }
}

async fn internal_put(
    State(state): State<AppState>,
    Path(key): Path<String>,
    Query(q): Query<TtlQuery>,
    body: Bytes,
) -> Result<StatusCode, AppError> {
    let ttl = q.ttl.map(Duration::from_secs);
    state.coordinator.local_put(key, body, ttl);
    Ok(StatusCode::NO_CONTENT)
}

async fn internal_delete(State(state): State<AppState>, Path(key): Path<String>) -> StatusCode {
    state.coordinator.local_delete(&key);
    StatusCode::NO_CONTENT
}

#[derive(Debug, Deserialize)]
struct TtlQuery {
    /// Time-to-live in seconds; absent = no expiry.
    ttl: Option<u64>,
}
