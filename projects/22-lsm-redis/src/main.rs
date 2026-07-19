//! LSM storage engine + Redis-compatible server — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, `/metrics`, opening the [`Engine`], the RESP TCP
//! accept loop, an optional background compactor, the HTTP sidecar, graceful shutdown)
//! is wired up for you. The learning lives in the modules marked `TODO(Vx)`:
//!   - V1 `resp.rs`        — the RESP wire codec (so real `redis-cli` connects)
//!   - V2 `wal.rs`         — the write-ahead log (durability before the ack)
//!   - V3 `memtable.rs`    — the sorted in-memory write buffer + tombstones
//!   - V4 `sstable.rs`     — the immutable, sorted on-disk file + block index
//!   - V5 `bloom.rs`       — per-SSTable bloom filters (skip files that can't hold a key)
//!   - V6 `compaction.rs`  — background merge (bound read/space/write amplification)
//!   - V7 `block_cache.rs` — a hand-built LRU over decoded SSTable blocks
//! The `engine.rs` orchestrator ties them together; its read/write paths are `todo!()`s.
//!
//! There is no external dependency: the filesystem (`DATA_DIR`) IS the database — no
//! Postgres, no Redis. The `docker compose` service is only a *reference* redis to run
//! `redis-cli` / `redis-benchmark` against and to A/B your semantics.
//!
//! Scaffold state: this compiles and serves. The HTTP sidecar (`/healthz`, `/stats`,
//! `/metrics`) works, and the RESP port *accepts* connections — but the first command a
//! client sends hits V1's `parse_command` `todo!()` and panics that connection's task
//! (the server lives on). Once V1 works, `PING`/`AUTH` answer; `SET`/`GET`/`DEL` then
//! reach the engine `todo!()`s in order. See SPEC.md.

mod block_cache;
mod bloom;
mod compaction;
mod engine;
mod error;
mod memtable;
mod metrics;
mod resp;
mod routes;
mod server;
mod sstable;
mod wal;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::TcpListener;
use tokio::sync::watch;
use tracing::info;

use engine::{Engine, EngineConfig};
use server::ServerConfig;

/// Redis' conventional port. We listen here so `redis-cli` with no arguments connects
/// straight to *your* server (the reference redis in compose is on the project-scoped
/// host port 6322 instead — `redis-cli -p 6322` — so the two never collide).
const DEFAULT_RESP_PORT: u16 = 6379;
/// The HTTP sidecar (`/healthz`, `/stats`, `/metrics`) — observation only, not data.
const DEFAULT_HTTP_PORT: u16 = 8080;
const DEFAULT_DATA_DIR: &str = "./data";
const DEFAULT_MEMTABLE_MAX_BYTES: usize = 4 * 1024 * 1024; // 4 MiB
const DEFAULT_BLOCK_SIZE_BYTES: usize = 4096; // 4 KiB
const DEFAULT_BLOOM_BITS_PER_KEY: usize = 10; // ~1% false-positive rate
const DEFAULT_L0_COMPACTION_TRIGGER: usize = 4;
const DEFAULT_BLOCK_CACHE_BYTES: usize = 8 * 1024 * 1024; // 8 MiB (0 disables)
const DEFAULT_MAX_REQUEST_BYTES: usize = 512 * 1024 * 1024; // redis proto-max-bulk-len
const DEFAULT_COMPACTION_INTERVAL_MS: u64 = 1000;

/// Shared state for the HTTP sidecar. The engine is behind an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<Engine>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,lsm_redis=debug");
    let metrics_handle = metrics::install();

    let resp_port: u16 = common_config::parse_or("RESP_PORT", DEFAULT_RESP_PORT);
    let http_port: u16 = common_config::parse_or("HTTP_PORT", DEFAULT_HTTP_PORT);

    let config = EngineConfig {
        data_dir: PathBuf::from(common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR)),
        memtable_max_bytes: common_config::parse_or(
            "MEMTABLE_MAX_BYTES",
            DEFAULT_MEMTABLE_MAX_BYTES,
        ),
        wal_sync: wal::parse_sync_policy(&common_config::or_default("WAL_SYNC", "everysec")),
        block_size_bytes: common_config::parse_or("BLOCK_SIZE_BYTES", DEFAULT_BLOCK_SIZE_BYTES),
        bloom_bits_per_key: common_config::parse_or(
            "BLOOM_BITS_PER_KEY",
            DEFAULT_BLOOM_BITS_PER_KEY,
        ),
        l0_compaction_trigger: common_config::parse_or(
            "L0_COMPACTION_TRIGGER",
            DEFAULT_L0_COMPACTION_TRIGGER,
        ),
        block_cache_bytes: common_config::parse_or("BLOCK_CACHE_BYTES", DEFAULT_BLOCK_CACHE_BYTES),
    };

    // Open (or recover) the store. Fully wired: on a fresh DATA_DIR this returns an
    // empty, serving engine — no vertical is required to start.
    let engine = Engine::open(config)?;

    // Connection-time policy for the RESP front-end.
    let requirepass = common_config::or_default("REQUIREPASS", "");
    let server_config = Arc::new(ServerConfig {
        requirepass: (!requirepass.is_empty()).then(|| Arc::from(requirepass.as_str())),
        max_request_bytes: common_config::parse_or("MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES),
    });
    if server_config.requirepass.is_some() {
        info!("RESP auth enabled (REQUIREPASS set): clients must AUTH before any command");
    }

    // Graceful shutdown fans out to background tasks via this watch channel.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let mut tasks = Vec::new();

    // The data plane: the RESP server. Real `redis-cli` connects here.
    let resp_addr = format!("0.0.0.0:{resp_port}");
    let resp_listener = TcpListener::bind(&resp_addr).await?;
    info!(%resp_addr, "RESP server listening (try: redis-cli -p {resp_port} ping)");
    tasks.push(tokio::spawn(server::serve(
        resp_listener,
        engine.clone(),
        server_config,
        shutdown_rx.clone(),
    )));

    // Background compaction is off by default so the bare scaffold doesn't spawn a loop
    // that panics on V6's `todo!()`. Flip RUN_COMPACTION=true once V4 (flush) + V6 work.
    if common_config::parse_or("RUN_COMPACTION", false) {
        let interval = Duration::from_millis(common_config::parse_or(
            "COMPACTION_INTERVAL_MS",
            DEFAULT_COMPACTION_INTERVAL_MS,
        ));
        tasks.push(tokio::spawn(compaction::compaction_loop(
            engine.clone(),
            interval,
            shutdown_rx.clone(),
        )));
        info!(?interval, "background compaction started");
    } else {
        info!("background compaction disabled (RUN_COMPACTION=false)");
    }

    // The HTTP sidecar runs in the foreground and owns the ctrl-c signal; when it
    // returns we tell the RESP + compaction tasks to drain.
    let state = AppState { engine };
    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));
    let http_addr = format!("0.0.0.0:{http_port}");
    let http_listener = TcpListener::bind(&http_addr).await?;
    info!(%http_addr, "HTTP sidecar listening (GET /healthz, /stats, /metrics)");

    axum::serve(http_listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Drain background tasks.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so the server can drain in-flight work.
///
/// TODO(SPEC · ship it): on shutdown, stop accepting connections, finish in-flight
/// commands, and flush the WAL (a final `fsync`) so an acknowledged write can never be
/// lost across a clean restart — then exit. A dirty exit must still be safe: that's what
/// WAL replay (V2) is for.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
