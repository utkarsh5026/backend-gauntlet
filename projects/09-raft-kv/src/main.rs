//! Distributed key-value store on Raft — entrypoint and wiring.
//!
//! The plumbing (config, the cluster topology from env, the node, the peer RPC
//! transport, the axum server, the driver task, graceful shutdown) is wired up
//! for you. The learning lives in the modules marked `TODO(Vx)`: leader election
//! (V1, `election.rs`), log replication + commit (V2, `replication.rs`), the
//! replicated KV state machine (V3, `store.rs`), and snapshots + compaction
//! (V4, `snapshot.rs`). See SPEC.md.
//!
//! A cluster is N of these processes, each with a distinct `NODE_ID` and a shared
//! `PEERS` map. There is no external dependency — each node persists its own state
//! to disk and reaches the others over HTTP. Scaffold state: this compiles and
//! serves. `GET /healthz` and `GET /status` work; the node idles as a follower
//! (the driver is a scaffold — see `node::run`), and the first client write or
//! inbound RPC hits a `todo!()` and panics. That panic is your worklist.
//!
//! ## Run a 3-node cluster locally
//! ```bash
//! PEERS=1=127.0.0.1:9001,2=127.0.0.1:9002,3=127.0.0.1:9003 NODE_ID=1 cargo run -p raft-kv
//! PEERS=1=127.0.0.1:9001,2=127.0.0.1:9002,3=127.0.0.1:9003 NODE_ID=2 cargo run -p raft-kv
//! PEERS=1=127.0.0.1:9001,2=127.0.0.1:9002,3=127.0.0.1:9003 NODE_ID=3 cargo run -p raft-kv
//! ```

mod election;
mod error;
mod log;
mod node;
mod peer;
mod replication;
mod routes;
mod rpc;
mod snapshot;
mod store;

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::Context;
use tracing::info;

use node::RaftNode;
use rpc::NodeId;

const DEFAULT_NODE_ID: NodeId = 1;
/// Single-node default cluster, so `cargo run -p raft-kv` boots without any env.
const DEFAULT_PEERS: &str = "1=127.0.0.1:9001";
const DEFAULT_DATA_DIR: &str = "./data";
/// Leader heartbeat cadence. Must stay well below the election-timeout floor.
const DEFAULT_HEARTBEAT_MS: u64 = 50;
/// Randomized election timeout window. The spread desynchronizes followers (V1).
const DEFAULT_ELECTION_MIN_MS: u64 = 150;
const DEFAULT_ELECTION_MAX_MS: u64 = 300;
/// Snapshot once the retained log passes this many entries (V4).
const DEFAULT_SNAPSHOT_THRESHOLD: u64 = 1000;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,raft_kv=debug");

    let node_id: NodeId = common_config::parse_or("NODE_ID", DEFAULT_NODE_ID);
    let peers_raw = common_config::or_default("PEERS", DEFAULT_PEERS);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);

    // Parse the cluster: id → client-facing address. This node must be in it.
    let cluster = parse_peers(&peers_raw)?;
    let self_addr = cluster
        .get(&node_id)
        .cloned()
        .with_context(|| format!("NODE_ID {node_id} not found in PEERS ({peers_raw:?})"))?;

    // The peer set is everyone but us.
    let mut peer_addrs = cluster.clone();
    peer_addrs.remove(&node_id);

    let config = node::config_from_env(
        common_config::parse_or("HEARTBEAT_MS", DEFAULT_HEARTBEAT_MS),
        common_config::parse_or("ELECTION_MIN_MS", DEFAULT_ELECTION_MIN_MS),
        common_config::parse_or("ELECTION_MAX_MS", DEFAULT_ELECTION_MAX_MS),
        common_config::parse_or("SNAPSHOT_THRESHOLD", DEFAULT_SNAPSHOT_THRESHOLD),
    );

    // Each node keeps its persistent Raft state under data/<node-id>/. Recovery on
    // open (restoring term/vote/log) is V1/V2 work — see log::RaftLog::open.
    let state_path = std::path::Path::new(&data_dir)
        .join(format!("node-{node_id}"))
        .join("raft-state");
    let raft_log = log::RaftLog::open(state_path)?;

    let node = Arc::new(RaftNode::new(
        node_id,
        config,
        self_addr.clone(),
        peer_addrs,
        raft_log,
    ));
    info!(
        node = node_id,
        cluster_size = node.cluster_size(),
        quorum = node.quorum(),
        %self_addr,
        "raft node initialized"
    );

    // Spawn the driver (the election/heartbeat clock). In the scaffold it idles;
    // implementing V1/V2 turns it into a real Raft node — see node::run.
    tokio::spawn(node.clone().run());

    let bind_port = port_of(&self_addr)?;
    let state = routes::AppState { node };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{bind_port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (PUT /kv/{{key}} to write, GET /status to watch the cluster)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Parse `PEERS` — a comma-separated `id=host:port` list — into an id→addr map.
fn parse_peers(raw: &str) -> anyhow::Result<HashMap<NodeId, String>> {
    let mut map = HashMap::new();
    for entry in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let (id, addr) = entry
            .split_once('=')
            .with_context(|| format!("bad PEERS entry {entry:?} (want `id=host:port`)"))?;
        let id: NodeId = id
            .trim()
            .parse()
            .with_context(|| format!("bad node id in {entry:?}"))?;
        map.insert(id, addr.trim().to_string());
    }
    anyhow::ensure!(!map.is_empty(), "PEERS is empty");
    Ok(map)
}

/// Extract the port from a `host:port` address for binding.
fn port_of(addr: &str) -> anyhow::Result<u16> {
    addr.rsplit_once(':')
        .and_then(|(_, p)| p.parse().ok())
        .with_context(|| format!("no port in address {addr:?}"))
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(V1/V2 graceful shutdown): on shutdown, persist any un-flushed Raft state
/// so a restart recovers a consistent term/vote/log and finds no torn tail.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
