//! Distributed job queue — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Postgres pool, the worker pool + reaper,
//! the axum API, graceful shutdown) is wired up for you. The learning lives in
//! the modules marked `TODO(Vx)`: the SKIP LOCKED claim engine (V1), the
//! visibility-timeout lease + reaper (V2), retries/backoff/DLQ (V3), and
//! scheduling via LISTEN/NOTIFY (V4). See SPEC.md.
//!
//! Scaffold state: this compiles and serves the enqueue API. `POST /jobs` will
//! `todo!()`-panic, and turning on `RUN_WORKERS` makes a worker panic on its
//! first claim — those panic messages are your worklist.

mod error;
mod handlers;
mod job;
mod lease;
mod metrics;
mod queue;
mod retry;
mod routes;
mod scheduler;
mod worker;

use std::sync::Arc;
use std::time::Duration;

use metrics_exporter_prometheus::PrometheusHandle;
use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::{info, warn};

use queue::Queue;
use retry::RetryPolicy;
use worker::WorkerConfig;

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_REAPER_INTERVAL: Duration = Duration::from_secs(10);
const DEFAULT_GAUGE_INTERVAL: Duration = Duration::from_secs(10);

#[derive(Clone)]
pub struct AppState {
    pub queue: Arc<Queue>,
    pub enqueue_token: Option<Arc<str>>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,job_queue=debug,sqlx=warn");
    let metrics_handle = metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let database_url = common_config::require("DATABASE_URL")?;
    let default_max_attempts: i32 = common_config::parse_or("DEFAULT_MAX_ATTEMPTS", 5);
    let db_max_connections: u32 = common_config::parse_or("DB_MAX_CONNECTIONS", 20);

    let pool = PgPoolOptions::new()
        .max_connections(db_max_connections)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    let queue = Queue::new(pool.clone(), default_max_attempts);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_WORKERS", false) {
        let concurrency: usize = common_config::parse_or("WORKER_CONCURRENCY", 4);
        let cfg = worker_config();

        for n in 0..concurrency {
            let id = format!("worker-{n}");
            let task = worker::run(id, Arc::clone(&queue), cfg.clone(), shutdown_rx.clone());
            tasks.push(tokio::spawn(task));
        }

        let reaper_interval = Duration::from_secs(common_config::parse_or(
            "REAPER_INTERVAL_SECS",
            DEFAULT_REAPER_INTERVAL.as_secs(),
        ));
        let task = lease::reap_loop(pool.clone(), reaper_interval, shutdown_rx.clone());
        tasks.push(tokio::spawn(task));

        let gauge_interval = Duration::from_secs(common_config::parse_or(
            "GAUGE_INTERVAL_SECS",
            DEFAULT_GAUGE_INTERVAL.as_secs(),
        ));
        let task = metrics::gauge_loop(
            pool.clone(),
            cfg.queue_name,
            gauge_interval,
            shutdown_rx.clone(),
        );
        tasks.push(tokio::spawn(task));
        info!(concurrency, "worker pool started");
    } else {
        info!("workers disabled (RUN_WORKERS=false): enqueue API only");
    }

    let enqueue_token: Option<Arc<str>> = std::env::var("ENQUEUE_TOKEN")
        .ok()
        .filter(|t| !t.is_empty())
        .map(Arc::from);
    if enqueue_token.is_none() {
        warn!("ENQUEUE_TOKEN unset — POST /jobs and requeue are UNAUTHENTICATED (dev only)");
    }

    let state = AppState {
        queue,
        enqueue_token,
    };
    serve(state, port, metrics_handle).await?;

    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

fn worker_config() -> WorkerConfig {
    WorkerConfig {
        queue_name: common_config::or_default("QUEUE", "default"),
        poll_interval: Duration::from_millis(common_config::parse_or("POLL_INTERVAL_MS", 1000_u64)),
        visibility_timeout: Duration::from_secs(common_config::parse_or(
            "VISIBILITY_TIMEOUT_SECS",
            30_u64,
        )),
        claim_batch: common_config::parse_or("CLAIM_BATCH", 10_i64),
        retry: RetryPolicy::default(),
    }
}

async fn serve(state: AppState, port: u16, metrics_handle: PrometheusHandle) -> anyhow::Result<()> {
    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /jobs to enqueue)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
