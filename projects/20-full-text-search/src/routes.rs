//! HTTP surface: index documents, search, refresh, merge, and read stats.
//!
//! The routing, request/response shapes, and the input guards are wired. What the
//! handlers call into — `engine.add_document`/`bulk` (V1→V2), `engine.search`
//! (V1→V5→V3), `engine.delete`/`force_merge` (V4) — is where the `todo!()`s live.
//! Run as-is and `GET /healthz`, `GET /_stats`, `POST /_refresh`, and
//! `POST /_forcemerge` all work; the first real index/search/delete hits a Vx todo
//! and panics — that panic message is the worklist.
//!
//! Document text is carried as UTF-8 over JSON. `_bulk` is newline-delimited JSON
//! (one document per line), like Elasticsearch's `_bulk`.

use std::time::Instant;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::Deserialize;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::doc::NewDocument;
use crate::error::AppError;
use crate::shard::EngineStats;
use crate::AppState;

/// Build the application router (everything except `/metrics`, which closes over the
/// Prometheus handle instead of `AppState` — see [`metrics_router`]).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        // Indexing.
        .route("/documents", post(index_document))
        .route("/documents/{id}", delete(delete_document))
        .route("/_bulk", post(bulk))
        // Search.
        .route("/search", get(search))
        // Admin: make buffered docs searchable, compact segments, read stats.
        .route("/_refresh", post(refresh))
        .route("/_forcemerge", post(force_merge))
        .route("/_stats", get(stats))
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

/// The `/metrics` scrape endpoint, kept separate because it closes over the
/// [`PrometheusHandle`] rather than `AppState`.
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

/// `POST /documents` — index one document (V1 analyze → buffered for refresh).
///
/// TODO(security): require a valid API key (from `API_KEYS`) before indexing — an
/// open index endpoint is an open disk for the whole internet. Keys are never logged.
async fn index_document(
    State(state): State<AppState>,
    Json(new): Json<NewDocument>,
) -> Result<(StatusCode, Json<serde_json::Value>), AppError> {
    if new.text.trim().is_empty() {
        return Err(AppError::BadRequest("document text is empty".into()));
    }
    let (shard, doc_id) = state.engine.add_document(new).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "shard": shard, "doc_id": doc_id })),
    ))
}

/// `POST /_bulk` — index many documents, one JSON object per line (NDJSON).
///
/// TODO(security): same as `index_document` — gate behind an API key.
async fn bulk(
    State(state): State<AppState>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, AppError> {
    let text = std::str::from_utf8(&body)
        .map_err(|_| AppError::BadRequest("bulk body is not valid UTF-8".into()))?;

    let mut docs = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let doc: NewDocument = serde_json::from_str(line)
            .map_err(|e| AppError::BadRequest(format!("bad document on line {}: {e}", i + 1)))?;
        docs.push(doc);
    }

    let indexed = state.engine.bulk(docs).await?;
    Ok(Json(json!({ "indexed": indexed })))
}

#[derive(Debug, Deserialize)]
struct SearchQuery {
    /// The raw query text (analyzed with the same analyzer as indexing).
    q: String,
    /// How many hits to return. Bounded so a client can't ask for the whole corpus.
    #[serde(default = "default_size")]
    size: usize,
}

fn default_size() -> usize {
    10
}

/// `GET /search?q=&size=` — rank documents for a query (V1 → V5 fan-out → V3 score).
/// Public (no auth): reads don't mutate the index.
async fn search(
    State(state): State<AppState>,
    Query(query): Query<SearchQuery>,
) -> Result<Json<serde_json::Value>, AppError> {
    let started = Instant::now();
    let size = query.size.clamp(1, 1000);
    let hits = state.engine.search(&query.q, size).await?;
    let took_ms = started.elapsed().as_millis();
    Ok(Json(json!({
        "took_ms": took_ms,
        "total": hits.len(),
        "hits": hits,
    })))
}

/// `DELETE /documents/{id}` — tombstone a document by its external id (V4).
///
/// TODO(security): gate behind an API key.
async fn delete_document(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<StatusCode, AppError> {
    if state.engine.delete(&id).await? {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(AppError::NotFound)
    }
}

/// `POST /_refresh` — flush buffered documents into segments so they become
/// searchable (V2). Returns how many documents were made searchable.
///
/// TODO(security): gate behind an API key.
async fn refresh(State(state): State<AppState>) -> Result<Json<serde_json::Value>, AppError> {
    let refreshed = state.engine.refresh_all().await?;
    Ok(Json(json!({ "refreshed": refreshed })))
}

/// `POST /_forcemerge` — compact every shard to a single segment, dropping
/// tombstoned docs (V4). Returns how many segments were merged away.
///
/// TODO(security): gate behind an API key.
async fn force_merge(State(state): State<AppState>) -> Result<Json<serde_json::Value>, AppError> {
    let merged = state.engine.force_merge().await?;
    Ok(Json(json!({ "merged_segments": merged })))
}

/// `GET /_stats` — per-shard + aggregate index stats. Fully wired (no vertical
/// needed), so it works on the bare scaffold.
async fn stats(State(state): State<AppState>) -> Json<EngineStats> {
    Json(state.engine.stats().await)
}
