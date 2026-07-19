//! WebRTC SFU (Selective Forwarding Unit) — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Prometheus recorder, the muxed UDP media socket, the
//! media pump, the signaling + admin HTTP server, graceful shutdown) is wired up for you. The
//! learning lives in the modules marked `TODO(Vx)`: ICE/STUN connectivity (V1, `ice.rs`),
//! selective RTP forwarding + per-subscriber rewriting (V2, `forward.rs`), simulcast layer
//! selection (V3, `simulcast.rs`), and bandwidth estimation (V4, `bwe.rs`). See SPEC.md.
//!
//! One process is one SFU. Clients reach it in two steps: they `POST` to the **signaling**
//! HTTP API (`/rooms/:room/publish` or `/subscribe`) to join the room graph and get ICE
//! credentials, then ICE-connect to the **media** UDP port and send/receive RTP. The admin
//! HTTP endpoints (`/healthz`, `/readyz`, `/status`, `/metrics`) share the HTTP server.
//!
//! Scaffold state: this compiles and serves. The signaling + admin endpoints work immediately
//! (you can create rooms and see the topology). The media plane idles until a real client
//! ICE-connects — the first STUN check it sends hits the V1 `StunMessage::parse` `todo!()`.
//! That panic is the worklist — a clean scaffold with only dead-code warnings otherwise.

mod admin;
mod bwe;
mod error;
mod forward;
mod ice;
mod metrics;
mod pump;
mod sfu;
mod signaling;
mod simulcast;
mod wire;

use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;

use tokio::net::UdpSocket;
use tokio::sync::watch;
use tracing::info;

use sfu::{Sfu, SfuConfig};

const DEFAULT_MEDIA_PORT: u16 = 7000;
const DEFAULT_HTTP_PORT: u16 = 8080;
const DEFAULT_MAX_ROOMS: usize = 64;
const DEFAULT_MAX_PEERS: usize = 64;
const DEFAULT_MIN_KBPS: u32 = 150;
const DEFAULT_START_KBPS: u32 = 1000;
const DEFAULT_MAX_KBPS: u32 = 4000;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,webrtc_sfu=debug");
    let metrics_handle = metrics::install();

    let media_port: u16 = common_config::parse_or("MEDIA_PORT", DEFAULT_MEDIA_PORT);
    let http_port: u16 = common_config::parse_or("HTTP_PORT", DEFAULT_HTTP_PORT);
    let public_ip: IpAddr = common_config::or_default("PUBLIC_IP", "127.0.0.1")
        .parse()
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));

    let cfg = SfuConfig {
        public_ip,
        media_port,
        max_rooms: common_config::parse_or("MAX_ROOMS", DEFAULT_MAX_ROOMS),
        max_peers_per_room: common_config::parse_or("MAX_PEERS_PER_ROOM", DEFAULT_MAX_PEERS),
        min_bitrate: common_config::parse_or::<u32>("MIN_KBPS", DEFAULT_MIN_KBPS) * 1000,
        start_bitrate: common_config::parse_or::<u32>("START_KBPS", DEFAULT_START_KBPS) * 1000,
        max_bitrate: common_config::parse_or::<u32>("MAX_KBPS", DEFAULT_MAX_KBPS) * 1000,
    };
    let sfu = Arc::new(Sfu::new(cfg));

    // The media plane: one muxed UDP socket for STUN + RTP + RTCP.
    let media_bind = format!("0.0.0.0:{media_port}");
    let socket = Arc::new(UdpSocket::bind(&media_bind).await?);
    info!(%media_bind, "media udp socket bound (STUN/RTP/RTCP muxed)");

    // Graceful shutdown is broadcast to the pump; axum has its own signal handler below.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The media pump: one long-lived task. A `todo!()` panic ends it but leaves HTTP up.
    let pump_task = {
        let socket = socket.clone();
        let sfu = sfu.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = pump::run(socket, sfu, rx).await {
                tracing::warn!(error = %e, "media pump ended with error");
            }
        })
    };

    // The HTTP plane: signaling (join/publish/subscribe) + admin (health/status/metrics).
    let http_addr = format!("0.0.0.0:{http_port}");
    let listener = tokio::net::TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "http listening (signaling /rooms + admin /healthz /status /metrics)");
    let app = admin::router(metrics_handle, sfu.clone()).merge(signaling::router(sfu.clone()));

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // HTTP server stopped (SIGTERM): tell the pump to drain, then wait for it.
    let _ = shutdown_tx.send(true);
    let _ = pump_task.await;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so the servers can drain in-flight work.
///
/// TODO(protocol / graceful shutdown): on shutdown the SFU should send an ICE/DTLS close (or
/// at least stop forwarding) so peers learn the session is gone; axum drains in-flight HTTP
/// requests, and the pump is signalled via the watch channel above.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
