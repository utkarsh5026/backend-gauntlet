//! HTTP surface: the producer + admin API. Workers consume the queue out of band
//! (see `worker.rs`); this is how jobs get *in* and how you inspect them.
//!
//! The router and extractors are wired; what the handlers call into —
//! `queue.enqueue` / `queue.get` — is where the V1 `todo!()`s live. Run as-is and
//! a `POST /jobs` panics with "V1: insert a job…", which is the worklist.

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
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

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/dlq", get(get_dlq))
        .route("/job/{id}/requeue", post(requeue_job))
        .route("/jobs", post(enqueue))
        .route("/jobs/{id}", get(get_job))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
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
