//! Distributed transcoding pipeline — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Postgres pool, the coordinator's scheduler
//! loop, the worker pool + lease reaper, the axum control-plane API, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)`: keyframe-aligned chunking (V1, `chunk.rs`), the job DAG + scheduler
//! (V2, `dag.rs` + the store in `job.rs`), the idempotent parallel transcode
//! workers (V3, `worker.rs`), and the seamless stitch/remux (V4, `stitch.rs`).
//! See SPEC.md.
//!
//! Scaffold state: this compiles and serves the control-plane API. `POST /jobs`
//! will `todo!()`-panic (V2 seeds the DAG), and turning on `RUN_WORKERS` makes the
//! scheduler + a worker panic on their first store call — those panic messages are
//! your worklist. `ffmpeg` / `ffprobe` must be on `PATH` (or set `FFMPEG_BIN` /
//! `FFPROBE_BIN`) once you start running real transcodes.

mod chunk;
mod dag;
mod error;
mod ffmpeg;
mod job;
mod routes;
mod stitch;
mod worker;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::info;

use job::{JobStore, PipelineConfig, Rendition};
use worker::{Worker, WorkerConfig};

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_WORK_DIR: &str = "./work";
/// Aim for ~6 s chunks — but the keyframe boundary always wins (V1).
const DEFAULT_TARGET_CHUNK_SECS: f64 = 6.0;

/// Shared application state, cloned into every request handler. The store and
/// config are behind `Arc`s, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub store: Arc<JobStore>,
    pub cfg: Arc<PipelineConfig>,
}

/// The default output ABR ladder, used when a job doesn't pin its own.
fn default_ladder() -> Vec<Rendition> {
    vec![
        Rendition {
            name: "1080p".into(),
            height: 1080,
            v_bitrate_kbps: 5000,
            a_bitrate_kbps: 128,
        },
        Rendition {
            name: "720p".into(),
            height: 720,
            v_bitrate_kbps: 2800,
            a_bitrate_kbps: 128,
        },
        Rendition {
            name: "480p".into(),
            height: 480,
            v_bitrate_kbps: 1400,
            a_bitrate_kbps: 96,
        },
    ]
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,transcode_pipeline=debug,sqlx=warn");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let database_url = common_config::require("DATABASE_URL")?;
    let db_max_connections: u32 = common_config::parse_or("DB_MAX_CONNECTIONS", 20);

    let cfg = Arc::new(PipelineConfig {
        work_dir: PathBuf::from(common_config::or_default("WORK_DIR", DEFAULT_WORK_DIR)),
        ffmpeg_bin: common_config::or_default("FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin: common_config::or_default("FFPROBE_BIN", "ffprobe"),
        target_chunk_secs: common_config::parse_or("TARGET_CHUNK_SECS", DEFAULT_TARGET_CHUNK_SECS),
        default_ladder: default_ladder(),
        max_attempts: common_config::parse_or("MAX_ATTEMPTS", 3),
    });

    let pool = PgPoolOptions::new()
        .max_connections(db_max_connections)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    let store = Arc::new(JobStore::new(pool.clone()));

    // Graceful shutdown is broadcast to every background task via this watch.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The scheduler + worker pool run only when asked, so the bare scaffold serves
    // the control-plane API cleanly. (The scheduler's first tick calls the V2/V3
    // store methods, which panic until you implement them — flip RUN_WORKERS=true
    // once the DAG store works.)
    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_WORKERS", false) {
        let sched_interval =
            Duration::from_millis(common_config::parse_or("SCHEDULER_INTERVAL_MS", 500_u64));
        tasks.push(tokio::spawn(dag::schedule_loop(
            store.clone(),
            sched_interval,
            shutdown_rx.clone(),
        )));

        let concurrency: usize = common_config::parse_or("WORKER_CONCURRENCY", 4);
        let wcfg = WorkerConfig {
            poll_interval: Duration::from_millis(common_config::parse_or(
                "POLL_INTERVAL_MS",
                500_u64,
            )),
            lease: Duration::from_secs(common_config::parse_or("LEASE_SECS", 120_u64)),
        };
        for n in 0..concurrency {
            let w = Worker::new(
                format!("worker-{n}"),
                store.clone(),
                cfg.clone(),
                wcfg.clone(),
            );
            tasks.push(tokio::spawn(w.run(shutdown_rx.clone())));
        }
        info!(concurrency, "scheduler + worker pool started");
    } else {
        info!("workers disabled (RUN_WORKERS=false): control-plane API only");
    }

    let state = AppState { store, cfg };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /jobs to submit a transcode)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Tell background tasks to drain, then wait for them to finish. A worker that
    // stops claiming lets its in-flight lease expire, so the reaper on another node
    // (or the next run) can pick the task up — nothing is lost mid-transcode.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests and background
/// tasks can stop claiming.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
