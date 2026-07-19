//! The admin / observability HTTP surface — **wired**, not a vertical.
//!
//! Liveness/readiness probes, a non-secret status blob (this node's role in the mesh, the placed
//! rooms, the open relay legs, the per-leg layer sets, active recordings), and the Prometheus
//! `/metrics` scrape. Shares the axum server with the app router (see `main`), closing over the
//! [`PrometheusHandle`] and the shared [`AppState`].

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde_json::{json, Value};
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

/// Liveness: the process is up. A supervisor restarts the node if this stops answering.
async fn healthz() -> &'static str {
    "ok"
}

/// Readiness: safe to receive traffic. TODO(observability): once consensus is wired, this should
/// reflect whether this node can reach a quorum (it can serve placement reads even in a minority,
/// but should signal degraded so a load balancer can prefer a node that can *place* rooms).
async fn readyz() -> &'static str {
    "ok"
}

/// `GET /status` — a small non-secret view of this node's mesh role + the global topology it sees.
async fn status(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "node": {
            "region": state.node.region,
            "node_id": state.node.node_id,
            "media_addr": state.node.media_addr().to_string(),
            "cascade_port": state.node.cascade_port,
            "peers": state.node.peer_count,
            "max_peers_per_room": state.node.max_peers_per_room,
        },
        "placement": {
            "role": state.placement.role(),
            "term": state.placement.term(),
            "leader": state.placement.leader(),
            "max_rooms": state.placement.config().max_rooms,
            "quorum": state.placement.config().quorum(),
            "rooms": state.placement.snapshot(),
        },
        "cascade": {
            "region": state.cascade.region(),
            "max_links": state.cascade.config().max_links,
            "legs": state.cascade.links(),
        },
        "routing": {
            "hysteresis_ticks": state.routing.config().hysteresis_ticks,
            "legs": state.routing.snapshot(),
        },
        "recording": {
            "segment_secs": state.recorder.config().segment_secs,
            "active": state.recorder.active(),
        },
    }))
}
