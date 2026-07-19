//! HTTP surface: the control-plane API. Workers consume the DAG out of band (see
//! `worker.rs`); this is how a job gets *submitted* and *inspected*.
//!
//! The router and extractors are wired; what the handlers call into —
//! `store.submit` / `store.get_job` — is where the V2 `todo!()`s live. Run as-is
//! and a `POST /jobs` panics with "V2: insert job…", which is the worklist.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::job::{JobId, NewJob};
use crate::AppState;

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/jobs", post(submit))
        .route("/jobs/{id}", get(get_job))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `POST /jobs` — submit a transcode job (V2 seeds its DAG).
///
/// TODO(security): authenticate this before doing anything — an open submit lets
/// anyone make your workers shell out to ffmpeg on arbitrary inputs. Also validate
/// the body: the `source` must resolve under `WORK_DIR` (no traversal), and the
/// ladder entries must be sane (bounded height/bitrate, non-empty names).
async fn submit(
    State(state): State<AppState>,
    Json(new): Json<NewJob>,
) -> Result<(StatusCode, Json<serde_json::Value>), AppError> {
    // Fall back to the server default ladder when the request doesn't pin one.
    let ladder = if new.ladder.is_empty() {
        state.cfg.default_ladder.clone()
    } else {
        new.ladder.clone()
    };
    if ladder.is_empty() {
        return Err(AppError::BadRequest("empty output ladder".into()));
    }

    let id = state.store.submit(&new, &ladder).await?;
    Ok((StatusCode::ACCEPTED, Json(json!({ "id": id }))))
}

/// `GET /jobs/{id}` — job status + per-status task counts, so a caller can watch
/// the DAG drain.
async fn get_job(
    State(state): State<AppState>,
    Path(id): Path<JobId>,
) -> Result<Json<serde_json::Value>, AppError> {
    let view = state.store.get_job(id).await?.ok_or(AppError::NotFound)?;
    let body = serde_json::to_value(view).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}
