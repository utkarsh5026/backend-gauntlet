//! The admin / observability HTTP surface — **wired**, not a vertical.
//!
//! Liveness/readiness probes (k8s wires these to the pod: `/healthz` for liveness, `/readyz`
//! for readiness so a pod is only sent traffic once its deps are reachable), a non-secret
//! status blob, and the Prometheus `/metrics` scrape. Shares the axum server with the app
//! router (see `main`), closing over the [`PrometheusHandle`] and the shared [`AppState`].

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::AppState;

/// Build the admin router: liveness, readiness, a status blob, and the metrics scrape.
pub fn router(metrics: PrometheusHandle, state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/status", get(status))
        .route(
            "/metrics",
            get(move || {
                let metrics = metrics.clone();
                async move { metrics.render() }
            }),
        )
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(state)
}

/// Liveness: the process is up. k8s restarts the pod if this stops answering.
async fn healthz() -> &'static str {
    "ok"
}

/// Readiness: safe to receive traffic. TODO(observability / k8s): once the deps are wired,
/// this should reflect whether Postgres/Redis/NATS are actually reachable so a pod that lost
/// a dependency is pulled from the Service instead of serving errors.
async fn readyz() -> &'static str {
    "ok"
}

/// `GET /status` — a small non-secret view of platform config + live topology.
async fn status(State(state): State<AppState>) -> Json<serde_json::Value> {
    let cfg = state.platform.config();
    Json(json!({
        "streams_live": state.platform.live_count(),
        "streams": state.platform.snapshot(),
        "ladder": cfg.ladder,
        "segment_secs": cfg.segment_secs,
        "part_secs": cfg.part_secs,
        "transcode": {
            "queue_depth": state.workers.queue_depth(),
            "max_replicas": state.workers.config().max_replicas,
        },
        "chat": { "active_channels": state.chat.active_channels() },
        "edge_origin": state.edge.origin_base(),
    }))
}
