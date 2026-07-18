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
mod directory;
mod error;
mod hub;
mod metrics;
mod presence;
mod protocol;
mod routes;

use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use tracing::info;

use backpressure::OverflowPolicy;
use cluster::ClusterBridge;
use directory::Directory;
use hub::Hub;
use presence::PresenceRegistry;
use protocol::ServerMessage;

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_OUTBOX_CAPACITY: usize = 64;
// TTL a healthy multiple of the client's heartbeat interval (routes.rs's
// `ClientMessage::Heartbeat`): tight enough to reap silent drops promptly,
// loose enough that one missed beat doesn't evict a live member.
const DEFAULT_PRESENCE_TTL_SECS: u64 = 30;
const DEFAULT_PRESENCE_SWEEP_INTERVAL_SECS: u64 = 10;

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
    /// Shared secret required on `GET /ws?token=...`. Empty means the server
    /// is misconfigured — every upgrade is rejected (fail closed), not "auth
    /// disabled".
    pub ws_auth_token: Arc<str>,
    /// The persistent roster (people/groups) behind the admin panel. `None`
    /// when `DATABASE_URL` is unset — the pub/sub core runs DB-free; only the
    /// `/admin` API needs this. Playground scaffolding, not a vertical.
    pub directory: Option<Directory>,
    /// The configured Redis URL (`REDIS_URL`), always resolved. Lets the
    /// `/debug/health` devtools probe check the bus for reachability independent
    /// of cluster mode; the bus is only *used* when `cluster` is `Some` (V4).
    pub redis_url: Arc<str>,
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
    let presence_ttl = Duration::from_secs(common_config::parse_or(
        "PRESENCE_TTL_SECS",
        DEFAULT_PRESENCE_TTL_SECS,
    ));
    let presence_sweep_interval = Duration::from_secs(common_config::parse_or(
        "PRESENCE_SWEEP_INTERVAL_SECS",
        DEFAULT_PRESENCE_SWEEP_INTERVAL_SECS,
    ));
    let ws_auth_token: Arc<str> = common_config::or_default("WS_AUTH_TOKEN", "").into();
    if ws_auth_token.is_empty() {
        tracing::warn!(
            "WS_AUTH_TOKEN is not set — every websocket upgrade will be rejected with 401"
        );
    }

    let hub = Arc::new(Hub::new());
    let presence = Arc::new(PresenceRegistry::new());

    tokio::spawn(sweep(
        Arc::clone(&hub),
        Arc::clone(&presence),
        presence_sweep_interval,
        presence_ttl,
    ));

    // The admin-panel roster (people/groups/memberships) lives in Postgres and
    // is *optional*: the pub/sub core (V1–V4) is deliberately store-free, so it
    // runs fine with no DB. Set DATABASE_URL to enable the `/admin` API.
    let directory = {
        let database_url = common_config::or_default("DATABASE_URL", "");
        if database_url.is_empty() {
            info!("directory: DATABASE_URL unset — /admin roster API disabled");
            None
        } else {
            let pool = PgPoolOptions::new()
                .max_connections(5)
                .connect(&database_url)
                .await?;
            sqlx::migrate!().run(&pool).await?;
            info!("directory: connected to postgres, migrations applied");
            Some(Directory::new(pool))
        }
    };

    // Resolve the Redis URL up front (not only in cluster mode) so /debug/health
    // can probe the bus for reachability even when we're single-node and never
    // actually bridge through it.
    let redis_url: Arc<str> =
        common_config::or_default("REDIS_URL", "redis://localhost:6303/0").into();

    let cluster = if cluster_enabled {
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
        ws_auth_token,
        directory,
        redis_url,
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

/// Background loop that reaps stale presence and fans out the survivors.
///
/// Every `interval`, calls [`PresenceRegistry::sweep`] with `ttl` to drop
/// members whose last heartbeat is too old (silent TCP drops that never sent
/// a leave). For each topic that actually changed, publishes a
/// [`ServerMessage::Presence`] so still-connected subscribers see the updated
/// roster without waiting for their next join/leave.
async fn sweep(hub: Arc<Hub>, presence: Arc<PresenceRegistry>, interval: Duration, ttl: Duration) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        ticker.tick().await;
        for (topic, members) in presence.sweep(ttl) {
            let members = members
                .into_iter()
                .map(|m| m.identity().to_string())
                .collect();
            let presence = ServerMessage::Presence {
                topic: topic.clone(),
                members,
            };
            hub.publish(&topic, presence);
        }
    }
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight work.
///
/// TODO(protocol): on shutdown, close live sockets with a proper WebSocket close
/// frame rather than dropping the TCP connections out from under clients.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
