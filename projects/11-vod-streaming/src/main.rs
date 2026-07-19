//! VOD streaming server (HLS/DASH) — entrypoint and wiring.
//!
//! The plumbing (config, scanning the media library, the axum router, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)`: the ISO-BMFF demuxer (V1, `isobmff.rs`), the fMP4/CMAF segmenter
//! (V2, `segment.rs`), the HLS/DASH manifest generator (V3, `manifest.rs`), and
//! byte-range delivery (V4, `delivery.rs`). See SPEC.md.
//!
//! There is no external dependency: the filesystem IS the source. Scaffold state:
//! this compiles and serves. `GET /healthz` and `GET /assets` work; the first
//! playlist/segment request hits a `todo!()` and panics — that panic message is
//! your worklist.

mod catalog;
mod delivery;
mod error;
mod isobmff;
mod manifest;
mod routes;
mod segment;

use std::sync::Arc;

use tracing::info;

use catalog::Catalog;

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_MEDIA_DIR: &str = "./media";
/// Aim for ~6 s segments (a common HLS/DASH default) — but never split a GOP to hit
/// it; the keyframe boundary wins (V2).
const DEFAULT_TARGET_SEGMENT_SECS: f64 = 6.0;

/// Shared application state, cloned into every request handler. The catalog is
/// behind an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub catalog: Arc<Catalog>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,vod_streaming=debug");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let media_dir = common_config::or_default("MEDIA_DIR", DEFAULT_MEDIA_DIR);
    let target_segment_secs: f64 =
        common_config::parse_or("TARGET_SEGMENT_SECS", DEFAULT_TARGET_SEGMENT_SECS);

    // Scan MEDIA_DIR/<asset>/<rendition>.mp4 into the catalog. The packaging
    // pipeline (demux → segment → manifest) hangs off this; its interesting steps
    // are the todo!()s in isobmff/segment/manifest.
    let catalog = Catalog::load(&media_dir, target_segment_secs).await?;
    info!(%media_dir, target_segment_secs, "media library ready");

    let state = AppState {
        catalog: Arc::new(catalog),
    };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (GET /assets to browse; /vod/{{asset}}/master.m3u8 to play)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(horizontal / graceful shutdown): in-flight segment streams should finish
/// before exit — axum's `with_graceful_shutdown` already drains active requests, so
/// once segment bodies are streamed (not buffered), a clean SIGTERM drops nothing.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
