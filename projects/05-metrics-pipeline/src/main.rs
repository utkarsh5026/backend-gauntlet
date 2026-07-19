//! Time-series metrics pipeline — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the NATS/JetStream connection + durable
//! stream, the ClickHouse client, the SSE broadcast hub, the optional consumer
//! pipeline, the axum API, graceful shutdown) is wired up for you. The learning
//! lives in the modules marked `TODO(Vx)`: the line-protocol parser + series
//! fingerprint (V1, `parse.rs`), the windowed rollup engine with a percentile
//! sketch (V2, `rollup.rs`), the batched at-least-once ClickHouse sink (V3,
//! `sink.rs`), and the SSE live fan-out (V4, `sse.rs`). See SPEC.md.
//!
//! Scaffold state: this compiles and serves the ingest API. `POST /ingest` will
//! `todo!()`-panic on the V1 parse and `GET /stream` on the V4 SSE; turning on
//! `RUN_CONSUMER` makes the pipeline panic on its first rollup. Those panic
//! messages are your worklist.

mod broker;
mod error;
mod model;
mod parse;
mod pipeline;
mod rollup;
mod routes;
mod sink;
mod sse;

use std::time::Duration;

use async_nats::jetstream;
use tokio::sync::watch;
use tracing::info;

use broker::Producer;
use pipeline::PipelineConfig;
use rollup::Rollup;
use sink::Sink;
use sse::LiveFeed;

const DEFAULT_PORT: u16 = 8080;

/// Shared application state, cloned into every request handler. Every field is
/// cheap to clone (handles / `Arc`-backed clients).
#[derive(Clone)]
pub struct AppState {
    /// Publishes ingested lines to the durable stream.
    pub producer: Producer,
    /// The SSE fan-out hub (also held by the consumer pipeline).
    pub feed: LiveFeed,
    /// Read-only ClickHouse handle for the `GET /query` path.
    pub ch: clickhouse::Client,
    /// The rollup table the query path reads from.
    pub rollup_table: String,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,metrics_pipeline=debug,async_nats=warn");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);

    // --- Broker: NATS JetStream (the durable log between ingest and consumer). ---
    let nats_url = common_config::require("NATS_URL")?;
    let stream_name = common_config::or_default("STREAM_NAME", "METRICS");
    let nats = async_nats::connect(&nats_url).await?;
    let js = jetstream::new(nats);
    broker::ensure_stream(&js, &stream_name).await?;
    info!(%nats_url, stream = %stream_name, "connected to NATS JetStream");

    // --- Store: ClickHouse (the queryable home for rolled-up metrics). ---
    let ch_url = common_config::require("CLICKHOUSE_URL")?;
    let ch_db = common_config::or_default("CLICKHOUSE_DB", "default");
    let ch_user = common_config::or_default("CLICKHOUSE_USER", "default");
    let ch_password = common_config::or_default("CLICKHOUSE_PASSWORD", "");
    let rollup_table = common_config::or_default("ROLLUP_TABLE", "metrics_rollup");
    let ch = clickhouse::Client::default()
        .with_url(&ch_url)
        .with_database(&ch_db)
        .with_user(&ch_user)
        .with_password(&ch_password);
    info!(%ch_url, db = %ch_db, "configured ClickHouse client");

    // The SSE broadcast hub — bounded, so a slow dashboard is shed not blocking.
    let feed = LiveFeed::new(common_config::parse_or("SSE_CAPACITY", 1024_usize));
    let producer = Producer::new(js.clone(), broker::RAW_SUBJECT);

    // Graceful shutdown is broadcast to background tasks via this watch.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The consumer pipeline runs only when asked, so the bare scaffold serves the
    // ingest API cleanly. (Its first point hits the V2 rollup `todo!()` until you
    // implement it — flip RUN_CONSUMER=true once V1/V2 work.)
    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_CONSUMER", false) {
        let window = Duration::from_secs(common_config::parse_or("WINDOW_SECS", 60_u64));
        let grace = Duration::from_secs(common_config::parse_or("GRACE_SECS", 10_u64));
        let rollup = Rollup::new(window, grace);

        let sink = Sink::new(
            ch.clone(),
            rollup_table.clone(),
            common_config::parse_or("BATCH_MAX_ROWS", 10_000_usize),
            Duration::from_millis(common_config::parse_or("BATCH_MAX_DELAY_MS", 1000_u64)),
        );

        let cfg = PipelineConfig {
            stream_name: stream_name.clone(),
            durable_name: common_config::or_default("DURABLE_NAME", "rollup-consumer"),
            flush_interval: Duration::from_millis(common_config::parse_or(
                "FLUSH_INTERVAL_MS",
                1000_u64,
            )),
        };

        tasks.push(tokio::spawn(pipeline::run(
            js.clone(),
            cfg,
            rollup,
            sink,
            feed.clone(),
            shutdown_rx.clone(),
        )));
        info!("consumer pipeline started");
    } else {
        info!("consumer disabled (RUN_CONSUMER=false): ingest API only");
    }

    let state = AppState {
        producer,
        feed,
        ch,
        rollup_table,
    };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /ingest to send metrics, GET /stream for the live feed)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Tell background tasks to drain (flush partial windows), then wait for them.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight work.
///
/// TODO(SPEC): on shutdown, stop accepting ingest first, then let the pipeline
/// flush its open rollup windows + in-flight batch to ClickHouse before exiting —
/// a clean stop shouldn't drop a partial window it could have flushed.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
