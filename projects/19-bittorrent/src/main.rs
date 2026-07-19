//! BitTorrent client + seeder — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, `/metrics`, the [`Client`] engine, the optional
//! seeder accept-loop, the axum control plane, graceful shutdown) is wired up for you.
//! The learning lives in the modules marked `TODO(Vx)`:
//!   - V1 `bencode.rs`  — the wire's data format (bencode encode/decode)
//!   - V2 `metainfo.rs` — parse `.torrent`/`magnet:` and compute the infohash
//!   - V3 `tracker.rs`  — announce over HTTP *and* UDP to discover peers
//!   - V4 `peer.rs`     — the peer wire protocol over raw TCP (handshake + messages)
//!   - V5 `download.rs` — piece picker + SHA-1 verify + write the file
//!   - V6 `seeder.rs`   — serve pieces under a choke algorithm (upload slots)
//!
//! Scaffold state: this compiles and serves the control plane. `POST /torrents`
//! `todo!()`-panics the moment it tries to parse the metainfo (V2), and turning on
//! `RUN_SEEDER` makes the accept-loop panic on its first inbound peer (V6) — those
//! panic messages are your worklist. See SPEC.md.

mod bencode;
mod client;
mod download;
mod error;
mod metainfo;
mod metrics;
mod peer;
mod routes;
mod seeder;
mod tracker;
mod types;

use std::sync::Arc;

use tokio::net::TcpListener;
use tokio::sync::watch;
use tracing::info;

use client::{generate_peer_id, Client, ClientConfig};

const DEFAULT_PORT: u16 = 8080;
/// BitTorrent clients conventionally listen for inbound peers on 6881–6889; the
/// project-scoped host default is 6819 (6881 with the last two digits → NN=19).
const DEFAULT_PEER_PORT: u16 = 6819;

/// Shared application state, cloned into every request handler. The engine is behind
/// an `Arc`, so cloning `AppState` is cheap.
#[derive(Clone)]
pub struct AppState {
    pub client: Arc<Client>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,bittorrent=debug");
    let metrics_handle = metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let peer_port: u16 = common_config::parse_or("PEER_PORT", DEFAULT_PEER_PORT);
    let download_dir = common_config::or_default("DOWNLOAD_DIR", "./data");
    let max_peers: usize = common_config::parse_or("MAX_PEERS", 50);
    let upload_slots: usize = common_config::parse_or("UPLOAD_SLOTS", 4);

    let cfg = ClientConfig {
        peer_id: generate_peer_id(),
        peer_port,
        download_dir: download_dir.into(),
        max_peers,
        upload_slots,
    };
    info!(peer_id = %cfg.peer_id, peer_port, "client identity");
    let client = Client::new(cfg);

    // Graceful shutdown is broadcast to every background task via this watch.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The seeder runs only when asked, so the bare scaffold serves the control plane
    // without an accept-loop panicking on its first (todo!()) inbound peer. Flip
    // RUN_SEEDER=true once V6 works.
    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_SEEDER", false) {
        let addr = format!("0.0.0.0:{peer_port}");
        let listener = TcpListener::bind(&addr).await?;
        info!(%addr, "seeder listening for inbound peers");
        tasks.push(tokio::spawn(seeder::accept_loop(
            listener,
            client.clone(),
            shutdown_rx.clone(),
        )));
    } else {
        info!("seeder disabled (RUN_SEEDER=false): control plane + leecher only");
    }

    let state = AppState { client };
    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let http_addr = format!("0.0.0.0:{port}");
    let http_listener = TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "control plane listening (POST /torrents, GET /torrents)");

    axum::serve(http_listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Tell background tasks to drain, then wait for them.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight work.
///
/// TODO(SPEC · ship it): on shutdown, stop accepting peers, flush any in-flight piece
/// writes, and announce `stopped` to every tracker before exiting — never leave a
/// half-written piece or a stale entry in the swarm.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
