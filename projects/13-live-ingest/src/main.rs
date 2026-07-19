//! Live ingest server (RTMP → LL-HLS) — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the shared live registry, the raw-TCP RTMP accept
//! loop, the axum LL-HLS delivery server, graceful shutdown) is wired up for you. The
//! learning lives in the modules marked `TODO(Vx)`: the RTMP handshake + chunk-stream
//! reader (V1, `rtmp.rs`), the AMF0 codec + publish state machine (V2, `amf.rs` +
//! `session.rs`), the live fMP4 repackager (V3, `fmp4.rs`), and the LL-HLS playlist +
//! blocking reload (V4, `llhls.rs`). See SPEC.md.
//!
//! Two servers run side by side over one shared `LiveRegistry`: RTMP producers on
//! `RTMP_PORT` write built parts into it; HTTP viewers on `HTTP_PORT` read them back.
//!
//! Scaffold state: this compiles and serves. `GET /healthz` and `GET /live` work; the
//! moment a broadcaster connects, the RTMP handshake hits a `todo!()` and that session
//! ends — that panic message is your worklist. No external dependency: the source is a
//! live socket, everything downstream is a bounded in-memory window.

mod amf;
mod error;
mod fmp4;
mod live;
mod llhls;
mod routes;
mod rtmp;
mod session;

use std::sync::Arc;

use tokio::sync::watch;
use tracing::info;

use live::{IngestConfig, LiveRegistry};

const DEFAULT_RTMP_PORT: u16 = 1935;
const DEFAULT_HTTP_PORT: u16 = 8080;
const DEFAULT_TARGET_PART_SECS: f64 = 0.3;
const DEFAULT_TARGET_SEGMENT_SECS: f64 = 4.0;
const DEFAULT_WINDOW_SEGMENTS: usize = 8;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,live_ingest=debug");

    let rtmp_port: u16 = common_config::parse_or("RTMP_PORT", DEFAULT_RTMP_PORT);
    let http_port: u16 = common_config::parse_or("HTTP_PORT", DEFAULT_HTTP_PORT);

    // Authorized stream keys: a comma-separated allow-list. Empty ⇒ any key (dev).
    let stream_keys: Vec<String> = common_config::or_default("STREAM_KEYS", "")
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();

    let cfg = Arc::new(IngestConfig {
        stream_keys,
        target_part_secs: common_config::parse_or("TARGET_PART_SECS", DEFAULT_TARGET_PART_SECS),
        target_segment_secs: common_config::parse_or(
            "TARGET_SEGMENT_SECS",
            DEFAULT_TARGET_SEGMENT_SECS,
        ),
        window_segments: common_config::parse_or("LIVE_WINDOW_SEGMENTS", DEFAULT_WINDOW_SEGMENTS),
    });
    if cfg.stream_keys.is_empty() {
        info!("STREAM_KEYS empty — accepting ANY publish key (dev only; do not run open in prod)");
    }

    let registry = Arc::new(LiveRegistry::new(cfg.clone()));

    // Graceful shutdown is broadcast to the RTMP accept loop (and its sessions) here;
    // axum has its own signal handler below.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // RTMP ingest: a raw-TCP listener, one task per connection (see session.rs).
    let rtmp_addr = format!("0.0.0.0:{rtmp_port}");
    let rtmp_listener = tokio::net::TcpListener::bind(&rtmp_addr).await?;
    info!(%rtmp_addr, "rtmp ingest listening (publish to rtmp://host/live/<key>)");
    let rtmp_task = tokio::spawn(session::accept_loop(
        rtmp_listener,
        registry.clone(),
        shutdown_rx,
    ));

    // LL-HLS delivery: the axum HTTP server.
    let http_addr = format!("0.0.0.0:{http_port}");
    let http_listener = tokio::net::TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "http delivery listening (GET /live/<key>/index.m3u8 to play)");
    let app = routes::router(registry.clone());

    axum::serve(http_listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // HTTP server stopped (SIGTERM): tell the RTMP loop to drain, then wait for it.
    let _ = shutdown_tx.send(true);
    let _ = rtmp_task.await;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so the servers can drain in-flight work.
///
/// TODO(protocol / graceful shutdown): on shutdown, a live publisher's current segment
/// should be finalized and its playlist closed with `#EXT-X-ENDLIST`, and held blocking
/// reloads should return rather than being cut mid-response (axum drains in-flight HTTP;
/// the RTMP side is signalled via the watch channel above).
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
