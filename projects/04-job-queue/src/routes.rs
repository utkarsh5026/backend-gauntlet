//! HTTP surface: the producer + admin API. Workers consume the queue out of band
//! (see `worker.rs`); this is how jobs get *in* and how you inspect them.
//!
//! The router and extractors are wired; what the handlers call into —
//! `queue.enqueue` / `queue.get` — is where the V1 `todo!()`s live. Run as-is and
//! a `POST /jobs` panics with "V1: insert a job…", which is the worklist.

use axum::extract::{DefaultBodyLimit, Path, Query, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::Deserialize;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::job::{JobId, NewJob};
use crate::AppState;

/// Page size for `GET /dlq` when the caller doesn't ask for one.
const DEFAULT_DLQ_LIMIT: i64 = 50;
/// Hard ceiling on `GET /dlq`'s page size — the caller can't ask for more, so an
/// unbounded DLQ can never be pulled into one response (the "cap everything the
/// caller controls" rule).
const MAX_DLQ_LIMIT: i64 = 200;

/// Query params for `GET /dlq` (`?limit=&offset=`); both optional.
#[derive(Debug, Deserialize)]
struct DlqPage {
    limit: Option<i64>,
    offset: Option<i64>,
}

const MAX_BODY_BYTES: usize = 256 * 1024;

pub fn router(state: AppState) -> Router {
    let protected = Router::new()
        .route(
            "/jobs",
            post(enqueue).layer(DefaultBodyLimit::max(MAX_BODY_BYTES)),
        )
        .route("/job/{id}/requeue", post(requeue_job))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_auth));

    let public = Router::new()
        .route("/healthz", get(healthz))
        .route("/dlq", get(get_dlq))
        .route("/jobs/{id}", get(get_job));

    public
        .merge(protected)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

/// Auth middleware for the mutating routes. Requires `Authorization: Bearer <token>`
/// matching [`AppState::enqueue_token`]; rejects with `401` otherwise. When no token
/// is configured (`None`) auth is disabled — `main` warns loudly at startup.
async fn require_auth(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, AppError> {
    let Some(expected) = state.enqueue_token.as_deref() else {
        return Ok(next.run(req).await);
    };
    let header_val = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());
    if bearer_matches(header_val, expected) {
        Ok(next.run(req).await)
    } else {
        Err(AppError::Unauthorized)
    }
}

/// True iff `auth_header` is exactly `Bearer <token>` with `token` matching `expected`.
fn bearer_matches(auth_header: Option<&str>, expected: &str) -> bool {
    match auth_header.and_then(|h| h.strip_prefix("Bearer ")) {
        Some(provided) => constant_time_eq(provided.as_bytes(), expected.as_bytes()),
        None => false,
    }
}

/// Length-checked constant-time byte comparison, so a caller can't recover the token
/// byte-by-byte from response-timing. The early length return leaks only the token's
/// length (not sensitive for a high-entropy secret); for equal lengths the loop never
/// short-circuits. Hand-rolled to avoid a workspace dependency (`subtle`).
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn metrics_router(handle: PrometheusHandle) -> Router {
    Router::new().route(
        "/metrics",
        get(move || {
            let handle = handle.clone();
            async move { handle.render() }
        }),
    )
}

async fn healthz() -> &'static str {
    "ok"
}

async fn enqueue(
    State(state): State<AppState>,
    Json(new): Json<NewJob>,
) -> Result<(StatusCode, Json<serde_json::Value>), AppError> {
    new.validate().map_err(AppError::BadRequest)?;
    let id = state.queue.enqueue(new).await?;
    Ok((StatusCode::CREATED, Json(json!({ "id": id }))))
}

async fn get_job(
    State(state): State<AppState>,
    Path(id): Path<JobId>,
) -> Result<Json<serde_json::Value>, AppError> {
    let job = state.queue.get(id).await?.ok_or(AppError::NotFound)?;
    let body = serde_json::to_value(job).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}

async fn get_dlq(
    State(state): State<AppState>,
    Query(page): Query<DlqPage>,
) -> Result<Json<serde_json::Value>, AppError> {
    let limit = page
        .limit
        .unwrap_or(DEFAULT_DLQ_LIMIT)
        .clamp(1, MAX_DLQ_LIMIT);
    let offset = page.offset.unwrap_or(0).max(0);

    let jobs = state.queue.get_dlq(limit, offset).await?;
    let body = serde_json::to_value(jobs).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}

async fn requeue_job(
    State(state): State<AppState>,
    Path(id): Path<JobId>,
) -> Result<Json<serde_json::Value>, AppError> {
    let job = state.queue.requeue(id).await?.ok_or(AppError::NotFound)?;
    let body = serde_json::to_value(job).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}

#[cfg(test)]
mod tests {
    //! Unit tests for the auth check. The bearer parsing + constant-time compare is
    //! the whole security-relevant surface; the middleware around it is thin glue,
    //! exercised end-to-end by the live `/jobs` smoke test (401 without a token, 201
    //! with the right one).
    use super::*;

    const TOKEN: &str = "s3cret-abc123";

    #[test]
    fn accepts_the_correct_bearer_token() {
        assert!(bearer_matches(Some("Bearer s3cret-abc123"), TOKEN));
    }

    #[test]
    fn rejects_wrong_token_same_and_different_length() {
        assert!(!bearer_matches(Some("Bearer s3cret-abc124"), TOKEN)); // one byte off
        assert!(!bearer_matches(Some("Bearer nope"), TOKEN)); // shorter
    }

    #[test]
    fn rejects_missing_or_wrong_scheme() {
        assert!(!bearer_matches(Some("s3cret-abc123"), TOKEN)); // no "Bearer " prefix
        assert!(!bearer_matches(Some("Basic s3cret-abc123"), TOKEN)); // wrong scheme
        assert!(!bearer_matches(Some("bearer s3cret-abc123"), TOKEN)); // case-sensitive
    }

    #[test]
    fn rejects_absent_header_and_empty_token() {
        assert!(!bearer_matches(None, TOKEN));
        assert!(!bearer_matches(Some("Bearer "), TOKEN)); // empty provided
        assert!(!bearer_matches(Some("Bearer s3cret-abc123"), "")); // empty expected
    }

    #[test]
    fn constant_time_eq_matches_only_identical_bytes() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"abcd")); // length mismatch
        assert!(constant_time_eq(b"", b""));
    }
}
