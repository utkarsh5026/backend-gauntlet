//! The HTTP sidecar: health, stats, and the Prometheus scrape.
//!
//! This is **not** the data plane — clients read and write over RESP (V1) on the redis
//! port. This tiny axum surface is how you *observe* the engine: a liveness probe, a
//! JSON stats dump, and `/metrics`. It's fully wired and works on the bare scaffold
//! (all three respond before any vertical exists), so you can watch memtable size and
//! SSTable counts move while you build the store.

use axum::routing::get;
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use tower_http::trace::TraceLayer;

use crate::engine::EngineStats;
use crate::AppState;

/// Build the sidecar router (everything except `/metrics`, which closes over the
/// Prometheus handle instead of `AppState` — see [`metrics_router`]).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/stats", get(stats))
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

/// `GET /stats` — a snapshot of engine internals (memtable size, SSTable count, block
/// cache hits/misses, sequence). Fully wired, so it works on the bare scaffold.
async fn stats(axum::extract::State(state): axum::extract::State<AppState>) -> Json<EngineStats> {
    Json(state.engine.stats())
}
