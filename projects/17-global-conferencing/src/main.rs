//! Global WebRTC conferencing (cascaded SFU) — entrypoint and wiring.
//!
//! This is a **capstone**: no new media primitive. One process is **one regional SFU**. It
//! composes the region-local SFU you built in project 15 (ICE/STUN, per-subscriber RTP rewrite,
//! simulcast selection, BWE) and the RTP transport under it (project 14) into a *global* cascade,
//! leaning on the Raft ideas from project 09 for room placement. The plumbing (config, telemetry,
//! the Prometheus recorder, the participant + backbone UDP sockets, the signaling + cluster-control
//! + admin HTTP server, graceful shutdown) is wired for you. The learning lives in the four modules
//! marked `TODO(Vx)`:
//!
//! - `placement.rs` (V1) — global room placement via consensus (one home region per room, ever).
//! - `cascade.rs`   (V2) — inter-SFU relay transport (one copy per region-pair, loop-free).
//! - `routing.rs`   (V3) — cross-region simulcast routing (carry the union of demand per leg).
//! - `recording.rs` (V4) — the recorder as a durable subscriber to the cascade.
//!
//! Scaffold state: this compiles and serves. `/healthz` and `/status` answer immediately; the
//! signaling and cluster routes exist but `todo!()`-panic when exercised (that panic message is
//! your worklist — the first `publish` tries to *place* the room, hitting the V1 todo). The
//! background loops (placement election, cascade pump) are gated behind `RUN_BACKGROUND=false` so
//! the bare scaffold boots without driving a consensus round with no peers — flip it on (and set
//! `PEERS`) once those verticals are implemented.

mod admin;
mod cascade;
mod error;
mod metrics;
mod placement;
mod recording;
mod routes;
mod routing;

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UdpSocket;
use tokio::sync::watch;
use tracing::info;

use cascade::{CascadeConfig, CascadeMesh};
use placement::{PeerNode, Placement, PlacementConfig};
use recording::{Recorder, RecordingConfig};
use routing::{LayerRouter, RoutingConfig};

const DEFAULT_HTTP_PORT: u16 = 8080;
const DEFAULT_MEDIA_PORT: u16 = 7000;
const DEFAULT_CASCADE_PORT: u16 = 7100;

/// Immutable per-node identity + limits, shared (behind an `Arc`) with the HTTP handlers.
pub struct NodeConfig {
    pub region: String,
    pub node_id: String,
    /// Host candidate address advertised to clients in signaling (project 15's `media_addr`).
    pub public_ip: IpAddr,
    pub media_port: u16,
    pub cascade_port: u16,
    pub max_peers_per_room: usize,
    /// Number of peer SFUs in the mesh (0 = a lone SFU / single region).
    pub peer_count: usize,
}

impl NodeConfig {
    /// The ICE host candidate this SFU advertises (where local clients send checks + media).
    pub fn media_addr(&self) -> SocketAddr {
        SocketAddr::new(self.public_ip, self.media_port)
    }
}

/// Shared application state, cloned into every request handler. Every field is an `Arc` handle,
/// so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    /// This node's identity + limits.
    pub node: Arc<NodeConfig>,
    /// V1 — the placement consensus (room → home region + membership).
    pub placement: Arc<Placement>,
    /// V2 — the inter-SFU cascade transport (relay legs).
    pub cascade: Arc<CascadeMesh>,
    /// V3 — the cross-region layer router (union of demand per leg).
    pub routing: Arc<LayerRouter>,
    /// V4 — the server-side recorder (a durable subscriber).
    pub recorder: Arc<Recorder>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,global_conferencing=debug");
    let metrics_handle = metrics::install();

    let region = common_config::or_default("REGION", "eu-west");
    let node_id = common_config::or_default("NODE_ID", "n1");
    let http_port: u16 = common_config::parse_or("HTTP_PORT", DEFAULT_HTTP_PORT);
    let media_port: u16 = common_config::parse_or("MEDIA_PORT", DEFAULT_MEDIA_PORT);
    let cascade_port: u16 = common_config::parse_or("CASCADE_PORT", DEFAULT_CASCADE_PORT);
    let public_ip: IpAddr = common_config::or_default("PUBLIC_IP", "127.0.0.1")
        .parse()
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));
    let run_background = common_config::parse_or("RUN_BACKGROUND", false);

    // The mesh: the other regional SFUs (V1 consensus peers + V2 relay targets).
    let peers = parse_peers(&common_config::or_default("PEERS", ""));
    info!(
        region,
        node_id,
        peers = peers.len(),
        "regional SFU starting"
    );

    let node = Arc::new(NodeConfig {
        region: region.clone(),
        node_id: node_id.clone(),
        public_ip,
        media_port,
        cascade_port,
        max_peers_per_room: common_config::parse_or("MAX_PEERS_PER_ROOM", 512_usize),
        peer_count: peers.len(),
    });

    // --- V1: placement consensus over the mesh (Raft-lite from project 09). ---
    let placement = Arc::new(Placement::new(PlacementConfig {
        region: region.clone(),
        node_id: node_id.clone(),
        peers: peers.clone(),
        election_timeout: Duration::from_millis(common_config::parse_or(
            "ELECTION_TIMEOUT_MS",
            1000_u64,
        )),
        heartbeat: Duration::from_millis(common_config::parse_or("HEARTBEAT_MS", 300_u64)),
        max_rooms: common_config::parse_or("MAX_ROOMS", 256_usize),
    }));

    // --- V2: the inter-SFU cascade transport. ---
    let cascade = Arc::new(CascadeMesh::new(CascadeConfig {
        region: region.clone(),
        cascade_port,
        peers: peers.clone(),
        max_links: common_config::parse_or("MAX_RELAY_LINKS", 16_usize),
    }));

    // --- V3: the cross-region layer router. ---
    let routing = Arc::new(LayerRouter::new(RoutingConfig {
        hysteresis_ticks: common_config::parse_or("HYSTERESIS_TICKS", 3_u32),
    }));

    // --- V4: the server-side recorder. ---
    let recorder = Arc::new(Recorder::new(RecordingConfig {
        dir: PathBuf::from(common_config::or_default("RECORDING_DIR", "./recordings")),
        segment_secs: common_config::parse_or("SEGMENT_SECS", 6.0_f64),
    }));

    let state = AppState {
        node: node.clone(),
        placement: placement.clone(),
        cascade: cascade.clone(),
        routing: routing.clone(),
        recorder: recorder.clone(),
    };

    // Graceful shutdown is broadcast to every background task via this watch. On SIGTERM the SFU
    // should relinquish leadership (so the mesh re-elects fast), tear down relay legs, and finalize
    // open recordings within the grace period.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // --- Media planes (UDP). ---
    // The backbone socket is this project's own transport (V2 relays SFU↔SFU on it).
    let cascade_bind = format!("0.0.0.0:{cascade_port}");
    let cascade_socket = Arc::new(UdpSocket::bind(&cascade_bind).await?);
    info!(%cascade_bind, "cascade udp socket bound (SFU<->SFU relay)");

    // The participant-facing muxed socket. Project 15's media pump (STUN/RTP/RTCP) mounts here
    // during integration; the scaffold binds it so the port is held.
    let media_bind = format!("0.0.0.0:{media_port}");
    let media_socket = Arc::new(UdpSocket::bind(&media_bind).await?);
    info!(%media_bind, "media udp socket bound (STUN/RTP/RTCP muxed — project 15 pump mounts here)");

    let mut tasks = Vec::new();

    // The background loops call into V1/V2 `todo!()`s and actively drive consensus, so they're
    // gated off by default: the bare scaffold serves the API without panicking. Flip
    // RUN_BACKGROUND=true (and set PEERS) once those verticals work to run a live mesh.
    if run_background {
        {
            let placement = placement.clone();
            let rx = shutdown_rx.clone();
            tasks.push(tokio::spawn(async move {
                if let Err(e) = placement.run(rx).await {
                    tracing::warn!(error = %e, "placement loop ended with error");
                }
            }));
        }
        {
            let cascade = cascade.clone();
            let socket = cascade_socket.clone();
            let rx = shutdown_rx.clone();
            tasks.push(tokio::spawn(async move {
                if let Err(e) = cascade.run(socket, rx).await {
                    tracing::warn!(error = %e, "cascade pump ended with error");
                }
            }));
        }
    }

    // Hold the participant media socket bound for the server's lifetime (p15's pump mounts here).
    {
        let socket = media_socket.clone();
        let mut rx = shutdown_rx.clone();
        tasks.push(tokio::spawn(async move {
            let _media_socket = socket;
            let _ = rx.changed().await;
        }));
    }

    // One HTTP server: signaling + cluster control (routes) merged with admin.
    let http_addr = format!("0.0.0.0:{http_port}");
    let listener = tokio::net::TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "http listening (signaling /rooms + cluster /cluster + admin /healthz /status /metrics)");
    let app = admin::router(metrics_handle, state.clone()).merge(routes::router(state));

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Server stopped (SIGTERM): tell background tasks to drain, then wait for them.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Parse the `PEERS` env var into the mesh's peer nodes.
///
/// Format: comma-separated `region=<http-control-base>|<cascade-udp-addr>`, e.g.
/// `us-east=http://10.0.0.2:8080|10.0.0.2:7100,ap-south=http://10.0.0.3:8080|10.0.0.3:7100`.
/// An empty string yields an empty mesh (a lone SFU — behaves like project 15).
fn parse_peers(raw: &str) -> Vec<PeerNode> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|entry| {
            let (region, rest) = entry.split_once('=')?;
            let (control, media) = rest.split_once('|')?;
            Some(PeerNode {
                region: region.trim().to_string(),
                control_addr: control.trim().to_string(),
                media_addr: media.trim().to_string(),
            })
        })
        .collect()
}

/// Waits for Ctrl-C / SIGTERM so the servers can drain in-flight work.
///
/// TODO(protocol / graceful shutdown): on shutdown the SFU should relinquish placement leadership
/// (V1) so the mesh re-elects quickly, tear down relay legs (V2), and finalize open recordings
/// (V4, `Recorder::flush_all`) within the grace period; axum drains in-flight HTTP requests, and
/// the background loops are signalled via the watch channel above.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
