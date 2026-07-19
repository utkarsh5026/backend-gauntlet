//! Real-time media transport (RTP/RTCP over UDP) — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Prometheus recorder, the UDP socket, the
//! per-role transport loop, the admin/metrics HTTP server, graceful shutdown) is wired up
//! for you. The learning lives in the modules marked `TODO(Vx)`: RTP packetization (V1,
//! `rtp.rs`), the jitter buffer (V2, `jitter.rs`), RTCP + NACK/retransmit (V3, `rtcp.rs`),
//! and congestion control (V4, `congestion.rs`). See SPEC.md.
//!
//! One process runs one role over one UDP socket. `ROLE=sender` needs `REMOTE_ADDR` and
//! pushes RTP toward it; `ROLE=receiver` (the default) binds and consumes. The admin HTTP
//! server (`/healthz`, `/readyz`, `/status`, `/metrics`) runs in both.
//!
//! Scaffold state: this compiles and serves. The admin endpoints work immediately. As a
//! receiver the process idles until an RTP datagram arrives, then hits the V1 `RtpPacket::parse`
//! `todo!()`; as a sender it produces a synthetic frame and hits the V1 packetize `todo!()`.
//! Those panics are the worklist — a clean scaffold with only dead-code warnings otherwise.

mod admin;
mod congestion;
mod error;
mod jitter;
mod media;
mod metrics;
mod rtcp;
mod rtp;
mod session;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UdpSocket;
use tokio::sync::watch;
use tracing::{info, warn};

use session::{Role, TransportConfig};

const DEFAULT_RTP_PORT: u16 = 5004;
const DEFAULT_HTTP_PORT: u16 = 8080;
/// Conservative UDP payload budget — stays under a 1500-byte path MTU with room for
/// IP/UDP headers (and a tunnel), so nothing relies on IP-layer fragmentation.
const DEFAULT_MTU: usize = 1200;
/// Dynamic payload type, typical for an H.264 mapping.
const DEFAULT_PAYLOAD_TYPE: u8 = 96;
const DEFAULT_PLAYOUT_MS: u64 = 100;
const DEFAULT_START_KBPS: u32 = 1500;
const DEFAULT_MIN_KBPS: u32 = 300;
const DEFAULT_MAX_KBPS: u32 = 4000;
const DEFAULT_FPS: u32 = 30;
const DEFAULT_GOP: u32 = 60;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,media_transport=debug");
    let metrics_handle = metrics::install();

    let rtp_port: u16 = common_config::parse_or("RTP_PORT", DEFAULT_RTP_PORT);
    let http_port: u16 = common_config::parse_or("HTTP_PORT", DEFAULT_HTTP_PORT);

    let role = match common_config::or_default("ROLE", "receiver")
        .to_ascii_lowercase()
        .as_str()
    {
        "sender" => Role::Sender,
        "receiver" => Role::Receiver,
        other => {
            warn!(role = other, "unknown ROLE, defaulting to receiver");
            Role::Receiver
        }
    };
    let remote_addr = std::env::var("REMOTE_ADDR")
        .ok()
        .and_then(|s| s.parse::<SocketAddr>().ok());
    if role == Role::Sender && remote_addr.is_none() {
        warn!("ROLE=sender but REMOTE_ADDR is unset/invalid — the sender will exit on start");
    }

    let cfg = Arc::new(TransportConfig {
        role,
        remote_addr,
        mtu: common_config::parse_or("MTU", DEFAULT_MTU),
        payload_type: common_config::parse_or("PAYLOAD_TYPE", DEFAULT_PAYLOAD_TYPE),
        target_playout: Duration::from_millis(common_config::parse_or(
            "PLAYOUT_MS",
            DEFAULT_PLAYOUT_MS,
        )),
        start_bitrate: common_config::parse_or::<u32>("START_KBPS", DEFAULT_START_KBPS) * 1000,
        min_bitrate: common_config::parse_or::<u32>("MIN_KBPS", DEFAULT_MIN_KBPS) * 1000,
        max_bitrate: common_config::parse_or::<u32>("MAX_KBPS", DEFAULT_MAX_KBPS) * 1000,
        fps: common_config::parse_or("FPS", DEFAULT_FPS),
        gop: common_config::parse_or("GOP", DEFAULT_GOP),
    });

    // The media plane: a single UDP socket both roles use (send and/or recv).
    let bind = format!("0.0.0.0:{rtp_port}");
    let socket = Arc::new(UdpSocket::bind(&bind).await?);
    info!(%bind, role = ?cfg.role, "rtp/udp socket bound");

    // Graceful shutdown is broadcast to the transport loop here; axum has its own signal
    // handler below.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The transport session: one long-lived task running the role's send/recv loop. A
    // panic in it (a `todo!()`) ends the task but leaves the admin server up.
    let session_task = {
        let socket = socket.clone();
        let cfg = cfg.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            let result = match cfg.role {
                Role::Sender => session::run_sender(socket, cfg.clone(), rx).await,
                Role::Receiver => session::run_receiver(socket, cfg.clone(), rx).await,
            };
            if let Err(e) = result {
                warn!(error = %e, "transport session ended with error");
            }
        })
    };

    // The admin/observability HTTP server.
    let http_addr = format!("0.0.0.0:{http_port}");
    let listener = tokio::net::TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "admin http listening (GET /healthz, /readyz, /status, /metrics)");
    let app = admin::router(metrics_handle, cfg.clone());

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // HTTP server stopped (SIGTERM): tell the transport loop to drain, then wait for it.
    let _ = shutdown_tx.send(true);
    let _ = session_task.await;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so the servers can drain in-flight work.
///
/// TODO(protocol / graceful shutdown): on shutdown a sender should send an RTCP **BYE** so
/// the peer learns the source is gone, and a receiver should drain its jitter buffer;
/// axum drains in-flight admin requests, and the transport loop is signalled via the watch
/// channel above.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
