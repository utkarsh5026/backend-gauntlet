//! Mini message broker (Kafka-lite) — entrypoint and wiring.
//!
//! The plumbing (config, the on-disk broker layout, the axum router, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)`: the segmented append-only log (V1, `log.rs`), the sparse offset
//! index (V2, `index.rs`), partitions + the partitioner (V3, `topic.rs`), and
//! consumer groups + durable offset commits (V4, `group.rs`). See SPEC.md.
//!
//! There is no external dependency: the filesystem IS the broker. Scaffold state:
//! this compiles and serves. `GET /healthz` works; the first real produce/fetch
//! hits a `todo!()` and panics — that panic message is your worklist.

mod broker;
mod error;
mod group;
mod index;
mod log;
mod partition;
mod record;
mod routes;
mod topic;

use std::sync::Arc;

use tracing::info;

use broker::Broker;
use log::LogConfig;

const DEFAULT_PORT: u16 = 9092;
const DEFAULT_DATA_DIR: &str = "./data";
/// Roll a segment past 64 MiB (small so tests roll segments; Kafka defaults to
/// 1 GiB). Must stay < 4 GiB — the sparse index stores u32 byte positions.
const DEFAULT_SEGMENT_BYTES: u64 = 64 * 1024 * 1024;
/// Emit a sparse index entry about every 4 KiB of log (Kafka's default).
const DEFAULT_INDEX_INTERVAL_BYTES: u64 = 4096;
/// Partitions for a topic created without an explicit count.
const DEFAULT_PARTITIONS: u32 = 3;
/// Reject a single record value larger than 1 MiB (Kafka's default ballpark).
const DEFAULT_MAX_RECORD_BYTES: u64 = 1024 * 1024;

/// Shared application state, cloned into every request handler. The broker is
/// behind an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub broker: Arc<Broker>,
    /// Per-record size cap, enforced on produce (security horizontal).
    pub max_record_bytes: u64,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,message_broker=debug");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);
    let segment_bytes: u64 = common_config::parse_or("SEGMENT_BYTES", DEFAULT_SEGMENT_BYTES);
    let index_interval_bytes: u64 =
        common_config::parse_or("INDEX_INTERVAL_BYTES", DEFAULT_INDEX_INTERVAL_BYTES);
    let default_partitions: u32 = common_config::parse_or("DEFAULT_PARTITIONS", DEFAULT_PARTITIONS);
    let max_record_bytes: u64 =
        common_config::parse_or("MAX_RECORD_BYTES", DEFAULT_MAX_RECORD_BYTES);

    // Open the on-disk layout under DATA_DIR: topics/<topic>/<partition>/ trees of
    // segment + index files (V1/V2), and groups/ for committed offsets (V4). The
    // constructor creates the directories and reloads any existing topics; the
    // interesting methods on each are the todo!()s.
    let config = LogConfig {
        segment_bytes,
        index_interval_bytes,
    };
    let broker = Broker::open(&data_dir, config, default_partitions)?;
    info!(
        %data_dir,
        segment_bytes,
        index_interval_bytes,
        default_partitions,
        "broker opened"
    );

    let state = AppState {
        broker,
        max_record_bytes,
    };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /topics then POST /topics/{{topic}}/records to produce)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(V1 / graceful shutdown): on shutdown, flush + fsync the active segments
/// and any uncommitted group offsets so a restart finds no torn tail and loses no
/// acknowledged write.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
