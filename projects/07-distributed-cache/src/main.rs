//! Distributed cache — entrypoint and wiring.
//!
//! The plumbing (config, the local store, the gossip socket, the axum router,
//! graceful shutdown) is wired for you. The learning lives in the modules marked
//! `TODO(Vx)`: the bounded LRU/LFU store (V1, `store.rs`), the consistent-hash
//! ring (V2, `ring.rs`), SWIM gossip membership (V3, `membership.rs`), and
//! replication + request coordination (V4, `coordinator.rs`). See SPEC.md.
//!
//! Each node is one instance of this binary; a "cluster" is several of them that
//! find each other via gossip seeds. Scaffold state: this compiles and serves.
//! `GET /healthz` and `GET /cluster` work (the node sees itself); the first real
//! `GET`/`PUT /cache/...` hits a `todo!()` and panics — that panic is your worklist.

mod coordinator;
mod error;
mod membership;
mod node;
mod ring;
mod routes;
mod store;

use std::net::SocketAddr;

use tracing::info;

use coordinator::Coordinator;
use membership::Membership;
use node::Node;
use store::{EvictionPolicy, Store};

const DEFAULT_HTTP_PORT: u16 = 8070;
const DEFAULT_GOSSIP_PORT: u16 = 7070;
const DEFAULT_CAPACITY: usize = 100_000;
const DEFAULT_VNODES: u32 = 128;
const DEFAULT_REPLICATION_FACTOR: usize = 2;
const DEFAULT_MAX_VALUE_BYTES: u64 = 1024 * 1024; // 1 MiB per entry

/// Shared application state, cloned into every handler. Each piece is behind an
/// `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub coordinator: std::sync::Arc<Coordinator>,
    pub membership: std::sync::Arc<Membership>,
    /// Per-entry value size cap, enforced at the edge before a write is routed.
    pub max_value_bytes: u64,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,distributed_cache=debug");

    // --- identity + config ----------------------------------------------------
    let http_port: u16 = common_config::parse_or("PORT", DEFAULT_HTTP_PORT);
    let gossip_port: u16 = common_config::parse_or("GOSSIP_PORT", DEFAULT_GOSSIP_PORT);
    // The address peers should use to reach us. In dev this is localhost; in the
    // docker-compose cluster it's the service name (cache-a, …).
    let advertise_host = common_config::or_default("ADVERTISE_HOST", "127.0.0.1");
    // Stable node id — defaults to host:port, but set NODE_ID explicitly so the
    // ring places the node consistently across restarts.
    let node_id = common_config::or_default("NODE_ID", format!("{advertise_host}:{http_port}"));

    let capacity: usize = common_config::parse_or("CACHE_CAPACITY", DEFAULT_CAPACITY);
    let policy: EvictionPolicy = common_config::or_default("EVICTION_POLICY", "lru")
        .parse()
        .map_err(|e: String| anyhow::anyhow!(e))?;
    let vnodes: u32 = common_config::parse_or("VNODES_PER_NODE", DEFAULT_VNODES);
    let replication_factor: usize =
        common_config::parse_or("REPLICATION_FACTOR", DEFAULT_REPLICATION_FACTOR);
    let max_value_bytes: u64 = common_config::parse_or("MAX_VALUE_BYTES", DEFAULT_MAX_VALUE_BYTES);

    let http_addr: SocketAddr = format!("0.0.0.0:{http_port}").parse()?;
    let gossip_addr: SocketAddr = format!("0.0.0.0:{gossip_port}").parse()?;
    // What we tell peers, using the advertised host rather than 0.0.0.0.
    let advertised = Node::new(
        node_id.clone(),
        format!("{advertise_host}:{http_port}").parse()?,
        format!("{advertise_host}:{gossip_port}").parse()?,
    );

    // Comma-separated `host:gossip_port` of seed peers to gossip a Join to.
    let seeds = parse_seeds(&common_config::or_default("SEEDS", ""))?;

    // --- build the node -------------------------------------------------------
    let store = Store::new(capacity, policy);
    info!(%node_id, capacity, ?policy, vnodes, replication_factor, "local store ready");

    // Membership binds the gossip UDP socket and seeds itself into the view.
    // We bind gossip on 0.0.0.0 but advertise the reachable address to peers.
    let gossip_node = Node::new(node_id.clone(), advertised.http_addr, gossip_addr);
    let membership = Membership::bind(gossip_node, seeds, vnodes).await?;

    let coordinator = Coordinator::new(
        node_id.clone(),
        store,
        membership.clone(),
        replication_factor,
    );

    // Drive SWIM in the background (receive loop now; probe ticker is a V3 TODO).
    tokio::spawn(membership.clone().run());

    let state = AppState {
        coordinator,
        membership,
        max_value_bytes,
    };
    let app = routes::router(state);

    let listener = tokio::net::TcpListener::bind(http_addr).await?;
    info!(%http_addr, %gossip_addr, "listening (PUT /cache/{{key}} to store; GET /cluster for membership)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Parse `SEEDS="host:port,host:port"` into socket addresses. Empty = no seeds
/// (this node is the first / a standalone).
fn parse_seeds(raw: &str) -> anyhow::Result<Vec<SocketAddr>> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.parse::<SocketAddr>()
                .map_err(|e| anyhow::anyhow!("bad seed address `{s}`: {e}"))
        })
        .collect()
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(V3 / graceful shutdown): before returning, gossip this node's departure
/// (broadcast it as leaving) so peers drop it immediately instead of waiting a
/// full suspicion timeout to notice it's gone.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
