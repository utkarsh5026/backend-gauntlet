//! Real-time pub/sub + presence — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the hub/presence registries, the optional
//! Redis cluster bridge, the axum server, graceful shutdown) is wired up for you.
//! The learning lives in the modules marked `TODO(Vx)`: the fan-out hub (V1), the
//! backpressure policy (V2), presence (V3), and the multi-node bus (V4). See
//! SPEC.md.
//!
//! Scaffold state: this compiles and serves. Open a socket and `publish`, and a
//! handler will `todo!()`-panic — that panic message is your worklist.

mod backpressure;
mod cluster;
mod error;
mod hub;
mod metrics;
mod presence;
mod protocol;
mod routes;

use std::sync::Arc;

use tracing::info;

use backpressure::OverflowPolicy;
use cluster::ClusterBridge;
use hub::Hub;
use presence::PresenceRegistry;

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_OUTBOX_CAPACITY: usize = 64;

/// Shared application state, cloned into every request handler. Everything here
/// is cheap to clone: the registries are behind `Arc`, the rest are small Copy
/// values.
#[derive(Clone)]
pub struct AppState {
    /// The in-process fan-out hub (V1).
    pub hub: Arc<Hub>,
    /// Per-topic presence (V3).
    pub presence: Arc<PresenceRegistry>,
    /// The cross-node bus (V4). `None` in single-node mode (`CLUSTER=false`).
    pub cluster: Option<Arc<ClusterBridge>>,
    /// How many messages one connection may buffer before its overflow policy
    /// kicks in (V2).
    pub outbox_capacity: usize,
    /// What a full outbox does to that connection (V2).
    pub overflow_policy: OverflowPolicy,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,realtime_pubsub=debug");
    let metrics_handle = metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let node_id = common_config::or_default("NODE_ID", "node-a");
    let cluster_enabled: bool = common_config::parse_or("CLUSTER", false);
    let outbox_capacity: usize =
        common_config::parse_or("OUTBOX_CAPACITY", DEFAULT_OUTBOX_CAPACITY);
    let overflow_policy: OverflowPolicy =
        common_config::parse_or("OVERFLOW_POLICY", OverflowPolicy::DropOldest);

    let hub = Arc::new(Hub::new());
    let presence = Arc::new(PresenceRegistry::new());

    let cluster = if cluster_enabled {
        let redis_url = common_config::or_default("REDIS_URL", "redis://localhost:6303/0");
        let bridge = Arc::new(ClusterBridge::connect(
            &redis_url,
            node_id.clone(),
            hub.clone(),
        )?);
        tokio::spawn(Arc::clone(&bridge).run());
        info!(%node_id, "cluster mode: bridged to redis bus");
        Some(bridge)
    } else {
        info!("single-node mode (CLUSTER=false): redis bus not used");
        None
    };

    let state = AppState {
        hub,
        presence,
        cluster,
        outbox_capacity,
        overflow_policy,
    };

    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, ?overflow_policy, outbox_capacity, "listening (ws at /ws)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight work.
///
/// TODO(protocol): on shutdown, close live sockets with a proper WebSocket close
/// frame rather than dropping the TCP connections out from under clients.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
