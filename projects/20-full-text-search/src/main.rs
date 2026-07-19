//! Full-text search engine (Elasticsearch-lite) — entrypoint and wiring.
//!
//! The plumbing (config, the sharded on-disk layout, the axum router + `/metrics`,
//! an optional background refresher, graceful shutdown) is wired up for you. The
//! learning lives in the modules marked `TODO(Vx)`: the analyzer (V1, `analyzer.rs`),
//! the on-disk inverted-index segments (V2, `segment.rs`), BM25 ranking (V3,
//! `bm25.rs`), segment merging + deletes (V4, `merge.rs`), and scatter-gather across
//! shards (V5, `shard.rs`). The query cache (`cache.rs`) is the caching horizontal.
//! See SPEC.md.
//!
//! There is no external dependency: the filesystem IS the index (no Postgres, no
//! Redis, no Elasticsearch). Scaffold state: this compiles and serves. `GET /healthz`,
//! `GET /_stats`, `POST /_refresh`, and `POST /_forcemerge` all work; the first real
//! index/search/delete hits a `todo!()` and panics — that panic message is the worklist.

mod analyzer;
mod bm25;
mod cache;
mod doc;
mod error;
mod index;
mod merge;
mod metrics;
mod routes;
mod segment;
mod shard;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tracing::{info, warn};

use analyzer::{Analyzer, AnalyzerConfig};
use bm25::Bm25Params;
use shard::{EngineConfig, ShardedIndex};

const DEFAULT_PORT: u16 = 9200; // Elasticsearch HTTP-port convention.
const DEFAULT_INDEX_DIR: &str = "./data";
const DEFAULT_SHARD_COUNT: u32 = 3;
const DEFAULT_MERGE_FACTOR: usize = 10;
const DEFAULT_MAX_DOC_BYTES: usize = 1024 * 1024; // 1 MiB
const DEFAULT_MAX_QUERY_TERMS: usize = 64;

/// Shared application state, cloned into every request handler. The engine is behind
/// an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<ShardedIndex>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,full_text_search=debug");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let index_dir = PathBuf::from(common_config::or_default("INDEX_DIR", DEFAULT_INDEX_DIR));
    let shard_count: u32 = common_config::parse_or("SHARD_COUNT", DEFAULT_SHARD_COUNT);
    if shard_count == 0 {
        anyhow::bail!("SHARD_COUNT must be >= 1");
    }
    let refresh_interval_ms: u64 = common_config::parse_or("REFRESH_INTERVAL_MS", 0);

    let config = EngineConfig {
        index_dir: index_dir.clone(),
        shard_count,
        bm25: Bm25Params {
            k1: common_config::parse_or("BM25_K1", 1.2_f32),
            b: common_config::parse_or("BM25_B", 0.75_f32),
        },
        merge_factor: common_config::parse_or("MERGE_FACTOR", DEFAULT_MERGE_FACTOR),
        max_doc_bytes: common_config::parse_or("MAX_DOC_BYTES", DEFAULT_MAX_DOC_BYTES),
        max_query_terms: common_config::parse_or("MAX_QUERY_TERMS", DEFAULT_MAX_QUERY_TERMS),
        query_cache_cap: common_config::parse_or("QUERY_CACHE_CAP", 0),
    };

    // One analyzer, shared by the index path and the query path (V1). Both run
    // identical analysis, which is what makes a query match a document.
    let analyzer = Arc::new(Analyzer::new(AnalyzerConfig::default()));

    // Open the sharded on-disk index: shard-<n>/ subdirectories under INDEX_DIR, each
    // an independent inverted index. Segments recover their own state on open (V2
    // recovery, deferred); the interesting methods are the todo!()s.
    let engine = ShardedIndex::open(config, analyzer)?;
    info!(
        %shard_count,
        index_dir = %index_dir.display(),
        "index opened"
    );

    // Prometheus recorder + a handle to render `/metrics`.
    let metrics_handle = metrics::install();

    // Graceful shutdown is broadcast to background tasks via this watch channel.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // Optional background refresher: flush each shard's buffer into a segment on a
    // fixed cadence (near-real-time search). Off by default (REFRESH_INTERVAL_MS=0)
    // so the bare scaffold serves without a task panicking on the V2 flush todo —
    // call POST /_refresh by hand instead. Turn it on once V2 works.
    let mut tasks = Vec::new();
    if refresh_interval_ms > 0 {
        tasks.push(tokio::spawn(refresh_loop(
            engine.clone(),
            Duration::from_millis(refresh_interval_ms),
            shutdown_rx.clone(),
        )));
        info!(refresh_interval_ms, "background refresher started");
    } else {
        info!("background refresher disabled (REFRESH_INTERVAL_MS=0): refresh via POST /_refresh");
    }

    let state = AppState { engine };
    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /documents to index, GET /search?q=… to query)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Tell the refresher to drain, then wait for it.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Periodically refresh every shard so buffered documents become searchable, until
/// shutdown is signalled.
async fn refresh_loop(
    engine: Arc<ShardedIndex>,
    interval: Duration,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                if let Err(e) = engine.refresh_all().await {
                    warn!(error = %e, "background refresh failed");
                }
            }
            _ = shutdown_rx.changed() => break,
        }
    }
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(graceful shutdown): on shutdown, flush each shard's buffer (a final refresh)
/// so buffered-but-unrefreshed documents aren't silently lost — decide and document
/// whether an un-refreshed document surviving a restart is a guarantee you make.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
