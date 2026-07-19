//! The admin / observability HTTP surface — **wired**, not a vertical.
//!
//! The media plane is UDP; this small axum server exists only for liveness/readiness
//! probes and the Prometheus `/metrics` scrape (the observability checklist). It closes
//! over the [`PrometheusHandle`] installed once in `main` (see [`crate::metrics`]) and a
//! read-only view of the [`TransportConfig`] for `/status`.

use std::sync::Arc;

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::session::TransportConfig;

#[derive(Clone)]
struct AdminState {
    cfg: Arc<TransportConfig>,
}

/// Build the admin router: liveness, readiness, a status blob, and the metrics scrape.
pub fn router(metrics: PrometheusHandle, cfg: Arc<TransportConfig>) -> Router {
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
        .with_state(AdminState { cfg })
}

async fn healthz() -> &'static str {
    "ok"
}

/// `GET /status` — a small non-secret view of how this process is configured.
async fn status(State(state): State<AdminState>) -> Json<serde_json::Value> {
    let cfg = &state.cfg;
    Json(json!({
        "role": format!("{:?}", cfg.role),
        "remote_addr": cfg.remote_addr.map(|a| a.to_string()),
        "mtu": cfg.mtu,
        "payload_type": cfg.payload_type,
        "target_playout_ms": cfg.target_playout.as_millis(),
        "bitrate_bps": { "start": cfg.start_bitrate, "min": cfg.min_bitrate, "max": cfg.max_bitrate },
    }))
}
