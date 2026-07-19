//! HTTP surface: the ingest endpoint, the live SSE feed, and historical query.
//!
//! The router and extractors are wired; what the handlers call into is where the
//! `todo!()`s live. Run as-is and `POST /ingest` panics with the V1 parse todo
//! and `GET /stream` with the V4 SSE todo — those panics are the worklist. The
//! consumer side (rollup → sink) is driven out of band by `pipeline.rs`.

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::AppState;
use crate::{parse, sink, sse};

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/ingest", post(ingest))
        .route("/stream", get(stream))
        .route("/query", get(query))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `POST /ingest` — accept a line-protocol body, validate it, and publish it to
/// the durable stream. Returns `202 Accepted`: the points are durably enqueued,
/// not yet rolled up or stored.
///
/// TODO(security): authenticate this (an API key) before publishing — an open
/// `/ingest` lets anyone forge metrics or blow up cardinality. Also cap the body
/// size and points-per-request (see SPEC: security).
async fn ingest(
    State(state): State<AppState>,
    body: axum::body::Bytes,
) -> Result<(StatusCode, Json<serde_json::Value>), AppError> {
    let text = std::str::from_utf8(&body)
        .map_err(|_| AppError::BadRequest("body is not valid UTF-8".into()))?;

    // V1: parse here to reject malformed input early with a 400 (rather than
    // letting a bad line into the durable stream). `parse` panics until V1 lands.
    let points = parse::parse(text)?;

    // The durable log holds the raw line as the source of truth; the consumer
    // re-parses it (see `pipeline.rs`). Publishing the bytes keeps the wire
    // format authoritative.
    state.producer.publish(body.clone()).await?;

    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "accepted": points.len() })),
    ))
}

/// `GET /stream` — Server-Sent Events feed of closed rollup windows (V4).
async fn stream(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let last_event_id = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    sse::stream(&state.feed, last_event_id).await
}

/// Query params for `GET /query?series=..&from=..&to=..` (unix seconds).
#[derive(Debug, Deserialize)]
struct QueryRange {
    series: u64,
    from: i64,
    to: i64,
}

/// `GET /query` — historical rollups for one series over a time range (V3 read
/// path), for the dashboard's initial paint before the SSE stream takes over.
async fn query(
    State(state): State<AppState>,
    Query(q): Query<QueryRange>,
) -> Result<Json<serde_json::Value>, AppError> {
    let from = chrono::DateTime::from_timestamp(q.from, 0)
        .ok_or_else(|| AppError::BadRequest("bad `from` timestamp".into()))?;
    let to = chrono::DateTime::from_timestamp(q.to, 0)
        .ok_or_else(|| AppError::BadRequest("bad `to` timestamp".into()))?;

    let rows = sink::query_range(&state.ch, &state.rollup_table, q.series, from, to).await?;
    let body = serde_json::to_value(rows).map_err(|e| AppError::Other(e.into()))?;
    Ok(Json(body))
}
