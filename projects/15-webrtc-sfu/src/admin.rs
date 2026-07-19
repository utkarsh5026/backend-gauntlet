//! The admin / observability HTTP surface — **wired**, not a vertical.
//!
//! Liveness/readiness probes, a non-secret status blob, and the Prometheus `/metrics`
//! scrape. It shares the axum server with the signaling router (see `main`), closing over
//! the [`PrometheusHandle`] installed once in `main` and a handle to the [`Sfu`] core for the
//! status topology.

use std::sync::Arc;

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::sfu::Sfu;

/// Build the admin router: liveness, readiness, a status blob, and the metrics scrape.
pub fn router(metrics: PrometheusHandle, sfu: Arc<Sfu>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(healthz))
        .route("/status", get(status))
        .route(
            "/metrics",
            get(move || {
                let metrics = metrics.clone();
                async move { metrics.render() }
            }),
        )
        .layer(TraceLayer::new_for_http().make_span_with(common_telemetry::make_request_span))
        .with_state(sfu)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /status` — a small non-secret view of how this SFU is configured + its live topology.
async fn status(State(sfu): State<Arc<Sfu>>) -> Json<serde_json::Value> {
    let cfg = sfu.config();
    Json(json!({
        "media_addr": cfg.media_addr().to_string(),
        "limits": { "max_rooms": cfg.max_rooms, "max_peers_per_room": cfg.max_peers_per_room },
        "bitrate_bps": { "min": cfg.min_bitrate, "start": cfg.start_bitrate, "max": cfg.max_bitrate },
        "topology": sfu.topology(),
    }))
}
