//! The consumer pipeline: consume → roll up → batch-sink → fan out. (Wiring.)
//!
//! This is glue — it *drives* the verticals and ties them into a lifecycle. It
//! pulls raw lines off the durable stream, parses them (V1), folds them into the
//! rollup engine (V2), flushes closed windows to the batched ClickHouse sink (V3)
//! and the live SSE feed (V4), and acks the broker **only after** a batch is
//! durably written (at-least-once). With the verticals still `todo!()`, the loop
//! panics on its first real point — that panic is the worklist. It runs only when
//! `RUN_CONSUMER=true` (see `main.rs`), so the bare scaffold serves ingest cleanly.

use std::time::Duration;

use async_nats::jetstream::{self, Context};
use futures_util::StreamExt;
use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::parse;
use crate::rollup::Rollup;
use crate::sink::Sink;
use crate::sse::LiveFeed;

/// Tuning for the consumer pipeline, assembled in `main.rs`.
pub struct PipelineConfig {
    /// JetStream stream name to bind the durable consumer to.
    pub stream_name: String,
    /// Durable consumer name — survives restarts so the offset is remembered.
    pub durable_name: String,
    /// How often to run the watermark flush + time-based batch flush.
    pub flush_interval: Duration,
}

/// Run the pipeline until shutdown is signalled.
///
/// Owns the `Rollup` engine and the `Sink` for its lifetime; `LiveFeed` is the
/// shared broadcast hub (also held by the SSE handlers).
pub async fn run(
    js: Context,
    cfg: PipelineConfig,
    mut rollup: Rollup,
    mut sink: Sink,
    feed: LiveFeed,
    mut shutdown: watch::Receiver<bool>,
) {
    info!(stream = %cfg.stream_name, durable = %cfg.durable_name, "consumer pipeline starting");

    // --- Bind a durable pull consumer to the raw-metrics stream (wiring). ---
    let mut messages = match setup_consumer(&js, &cfg).await {
        Ok(m) => m,
        Err(e) => {
            error!(error = %e, "failed to create JetStream consumer; pipeline aborting");
            return;
        }
    };

    let mut flush_ticker = tokio::time::interval(cfg.flush_interval);

    loop {
        tokio::select! {
            // A new message from the durable stream.
            next = messages.next() => {
                let Some(msg) = next else {
                    warn!("consumer stream ended");
                    break;
                };
                match msg {
                    Ok(msg) => process_message(&mut rollup, &mut sink, &msg).await,
                    Err(e) => error!(error = %e, "consumer recv error"),
                }
            }

            // Periodic: close windows whose watermark passed, push to sink + feed,
            // and honour the sink's time-based flush trigger.
            _ = flush_ticker.tick() => {
                flush_windows(&mut rollup, &mut sink, &feed).await;
                if let Err(e) = sink.flush().await {
                    error!(error = %e, "time-triggered sink flush failed");
                }
            }

            // Graceful shutdown: drain partial windows, do a final flush, exit.
            _ = shutdown.changed() => {
                info!("pipeline draining on shutdown");
                flush_windows(&mut rollup, &mut sink, &feed).await; // TODO(V2): drain_all here
                if let Err(e) = sink.flush().await {
                    error!(error = %e, "final sink flush failed");
                }
                break;
            }
        }
    }

    info!("consumer pipeline stopped");
}

/// Decode one message into points (V1), fold them into the rollup engine (V2),
/// and ack the broker. NOTE the ack ordering is the at-least-once contract (V3):
/// a real implementation must ack only once the resulting rollups are durably
/// written — see SPEC V3.
async fn process_message(rollup: &mut Rollup, sink: &mut Sink, msg: &jetstream::Message) {
    let _ = sink; // the durable write + ack ordering is the V3 lesson (see SPEC).

    let body = String::from_utf8_lossy(&msg.payload);
    // V1: re-parse the durable line into points. (You could instead publish
    // already-parsed points to skip this; that's a design choice — SPEC V1.)
    match parse::parse(&body) {
        Ok(points) => {
            for p in &points {
                rollup.ingest(p); // V2
            }
        }
        Err(e) => warn!(error = %e, "dropping unparseable message from stream"),
    }

    // TODO(V3): ack only AFTER the rollups these points feed have been flushed
    // and durably written — acking here (before the sink write) would turn a
    // crash into silent data loss. Move the ack into the flush path.
    if let Err(e) = msg.ack().await {
        error!(error = %e, "failed to ack message");
    }
}

/// Flush closed windows (V2) into the batched sink (V3) and the live feed (V4).
async fn flush_windows(rollup: &mut Rollup, sink: &mut Sink, feed: &LiveFeed) {
    let rows = rollup.flush_ready(chrono::Utc::now()); // V2
    if rows.is_empty() {
        return;
    }
    debug!(count = rows.len(), "flushing closed windows");

    for row in &rows {
        feed.publish(row.clone()); // V4: live fan-out (drop-tolerant)
    }
    if let Err(e) = sink.push(rows).await {
        // V3: must NOT drop on the durable path — log and let redelivery retry.
        error!(error = %e, "sink push failed; rollups will be retried via redelivery");
    }
}

/// Create the durable pull consumer and return its message stream. (Wiring.)
async fn setup_consumer(
    js: &Context,
    cfg: &PipelineConfig,
) -> anyhow::Result<jetstream::consumer::pull::Stream> {
    let stream = js.get_stream(&cfg.stream_name).await?;
    let consumer = stream
        .get_or_create_consumer(
            &cfg.durable_name,
            jetstream::consumer::pull::Config {
                durable_name: Some(cfg.durable_name.clone()),
                // Explicit ack is what makes at-least-once possible (V3): an
                // unacked message is redelivered after its ack-wait elapses.
                ack_policy: jetstream::consumer::AckPolicy::Explicit,
                ..Default::default()
            },
        )
        .await?;
    Ok(consumer.messages().await?)
}
