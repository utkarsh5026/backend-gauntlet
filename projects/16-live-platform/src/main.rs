//! Live streaming platform (Twitch-lite) — entrypoint and wiring.
//!
//! This is the **capstone**: no new primitive, it composes the pieces you built earlier into
//! one glass-to-glass pipeline — RTMP/WebRTC ingest → ABR transcode ladder → LL-HLS packaging
//! → edge delivery → realtime chat & presence — and runs it on k8s with autoscaling transcode
//! workers. The plumbing (config, telemetry, the Postgres control-plane pool, the Redis chat
//! bus, the NATS transcode queue, the axum server, graceful shutdown) is wired for you. The
//! learning lives in the four modules marked `TODO(Vx)`:
//!
//! - `control.rs`  (V1) — the stream session state machine + reconciliation (the brain).
//! - `workers.rs`  (V2) — the transcode queue, the visibility-timeout lease, and the queue-depth
//!                        signal an HPA scales the worker Deployment on.
//! - `edge.rs`     (V3) — LL-HLS blocking playlist reload + single-flight segment fan-out.
//! - `chat.rs`     (V4) — per-channel WebSocket fan-out, backpressure, presence, cross-node bus.
//!
//! Scaffold state: this compiles and serves. `/healthz` and `/status` answer immediately; the
//! ingest webhook, playback, and chat routes exist but `todo!()`-panic when exercised (that
//! panic message is your worklist). The background loops (`reconcile`, the transcode queue
//! setup, the chat bus) are gated behind `RUN_BACKGROUND=false` so the bare scaffold boots
//! cleanly — flip it on once the verticals they call are implemented.

mod admin;
mod chat;
mod control;
mod edge;
mod error;
mod metrics;
mod routes;
mod workers;

use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::info;

use chat::ChatHub;
use control::{Platform, PlatformConfig, Rendition};
use edge::EdgeCache;
use workers::{WorkerConfig, WorkerPool};

const DEFAULT_PORT: u16 = 8080;

/// Shared application state, cloned into every request handler. Every field is an
/// `Arc` handle, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    /// V1 — the control plane (session lifecycle over Postgres).
    pub platform: Arc<Platform>,
    /// V2 — the autoscaling transcode worker pool (over NATS JetStream).
    pub workers: Arc<WorkerPool>,
    /// V3 — the LL-HLS edge in front of the packager origin.
    pub edge: Arc<EdgeCache>,
    /// V4 — the chat & presence hub (per-channel fan-out over Redis).
    pub chat: Arc<ChatHub>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,live_platform=debug,sqlx=warn,async_nats=warn");
    let metrics_handle = metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);

    // --- Control plane: Postgres (durable stream sessions; source of truth). ---
    let database_url = common_config::require("DATABASE_URL")?;
    let pool = PgPoolOptions::new()
        .max_connections(common_config::parse_or("DB_MAX_CONNECTIONS", 16))
        .connect(&database_url)
        .await?;
    info!("connected to postgres (control plane)");

    let platform = Arc::new(Platform::new(
        PlatformConfig {
            max_streams: common_config::parse_or("MAX_STREAMS", 1000_usize),
            ladder: default_ladder(),
            segment_secs: common_config::parse_or("SEGMENT_SECS", 4.0_f64),
            part_secs: common_config::parse_or("PART_SECS", 0.5_f64),
        },
        pool,
    ));

    // --- Transcode queue: NATS JetStream (durable log between ingest and workers). ---
    let nats_url = common_config::require("NATS_URL")?;
    let nats = async_nats::connect(&nats_url).await?;
    let js = async_nats::jetstream::new(nats);
    info!(%nats_url, "connected to NATS JetStream (transcode queue)");

    let workers = Arc::new(WorkerPool::new(
        WorkerConfig {
            stream_name: common_config::or_default("TRANSCODE_STREAM", "TRANSCODE"),
            lease: Duration::from_secs(common_config::parse_or("TRANSCODE_LEASE_SECS", 60_u64)),
            target_backlog_per_worker: common_config::parse_or(
                "TARGET_BACKLOG_PER_WORKER",
                4_usize,
            ),
            max_replicas: common_config::parse_or("MAX_TRANSCODE_REPLICAS", 50_usize),
        },
        js,
    ));

    // --- Edge: fronts the packager origin. ---
    let edge = Arc::new(EdgeCache::new(common_config::or_default(
        "PACKAGER_ORIGIN",
        "http://localhost:9000",
    )));

    // --- Chat: Redis pub/sub bus for cross-node fan-out. ---
    let redis_url = common_config::require("REDIS_URL")?;
    let redis = redis::Client::open(redis_url.as_str())?
        .get_connection_manager()
        .await?;
    info!(%redis_url, "connected to redis (chat bus)");

    let chat = Arc::new(ChatHub::new(
        common_config::parse_or("OUTBOX_CAPACITY", 256_usize),
        common_config::or_default("NODE_ID", "node-a"),
        redis,
    ));

    let state = AppState {
        platform: platform.clone(),
        workers: workers.clone(),
        edge: edge.clone(),
        chat: chat.clone(),
    };

    // Graceful shutdown is broadcast to every background task via this watch. On
    // SIGTERM k8s gives the pod its `terminationGracePeriod` to drain — that window
    // is what lets an in-flight transcode finish and chat sockets close cleanly.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The background loops (control-plane reconcile, transcode queue setup, chat bus)
    // call into V1/V2/V4 `todo!()`s, so they're gated off by default: the bare scaffold
    // serves the API without panicking. Flip RUN_BACKGROUND=true once those verticals work.
    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_BACKGROUND", false) {
        platform.reconcile().await?;
        workers.ensure_queue().await?;

        let bus_chat = chat.clone();
        let mut rx = shutdown_rx.clone();
        tasks.push(tokio::spawn(async move {
            tokio::select! {
                res = bus_chat.run_bus() => {
                    if let Err(e) = res {
                        tracing::warn!(error = %e, "chat bus ended with error");
                    }
                }
                _ = rx.changed() => info!("chat bus shutting down"),
            }
        }));
    }

    // One axum server: the app routes (ingest + playback + chat) merged with admin.
    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "http listening (/ingest /live /chat + admin /healthz /status /metrics)");
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

/// The default ABR ladder if `ABR_LADDER` isn't overridden. Three rungs a player
/// adapts between — source-ish down to a mobile-friendly floor.
///
/// TODO(V1 / config): parse a custom ladder from the `ABR_LADDER` env var so the
/// rungs (and the transcode jobs they imply) are configurable per deployment.
fn default_ladder() -> Vec<Rendition> {
    vec![
        Rendition {
            name: "1080p".into(),
            width: 1920,
            height: 1080,
            bitrate_kbps: 6000,
        },
        Rendition {
            name: "720p".into(),
            width: 1280,
            height: 720,
            bitrate_kbps: 3000,
        },
        Rendition {
            name: "480p".into(),
            width: 854,
            height: 480,
            bitrate_kbps: 1200,
        },
    ]
}

/// Waits for Ctrl-C / SIGTERM so the server can drain in-flight work.
///
/// TODO(protocol / graceful shutdown): on shutdown the platform should stop admitting new
/// ingests and let in-flight transcodes finish within the k8s grace period; axum drains
/// in-flight HTTP requests, and background tasks are signalled via the watch channel above.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
