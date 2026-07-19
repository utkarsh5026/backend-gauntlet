//! V3 — The durable, batched sink: at-least-once into ClickHouse.
//!
//! Get rollups out of memory and into the column store **in batches** and
//! **durably**. A column store like ClickHouse is built for big appends and
//! falls over on row-at-a-time inserts, so the lesson is micro-batching: buffer
//! rollups and flush on a **size or time** trigger. Durability is at-least-once —
//! the pipeline acks the broker message only *after* a batch is safely written,
//! which means a crash causes redelivery, which means duplicates, which is why
//! the write must be **idempotent** (a dedup/merge key on `(series, window)`).
//!
//! This module owns the ClickHouse client and the batch buffer. The consume/ack
//! loop that drives it is wiring in `pipeline.rs`.

use std::time::Duration;

use crate::error::AppError;
use crate::model::RollupRow;

/// Writes batches of rollups to ClickHouse.
///
/// Construct it with a connected client (wired in `main.rs`). The batching
/// policy — when to flush — is the V3 lesson and lives in [`Sink::push`] /
/// [`Sink::flush`].
pub struct Sink {
    client: clickhouse::Client,
    /// Flush when the buffer reaches this many rows.
    batch_max_rows: usize,
    /// Flush at least this often even if the buffer isn't full (the latency
    /// half of the size-or-time trigger).
    batch_max_delay: Duration,
    /// The destination table (e.g. `metrics_rollup`).
    table: String,
    /// Pending rollups not yet flushed.
    buffer: Vec<RollupRow>,
}

impl Sink {
    pub fn new(
        client: clickhouse::Client,
        table: impl Into<String>,
        batch_max_rows: usize,
        batch_max_delay: Duration,
    ) -> Self {
        Self {
            client,
            batch_max_rows,
            batch_max_delay,
            table: table.into(),
            buffer: Vec::new(),
        }
    }

    /// Add rollups to the batch buffer, flushing if the size trigger is hit.
    /// Returns whether a flush happened (so the caller can ack the broker).
    pub async fn push(&mut self, rows: Vec<RollupRow>) -> Result<bool, AppError> {
        // TODO(V3): append to `self.buffer`; if `self.buffer.len() >=
        // self.batch_max_rows`, call `self.flush()` and return true. Otherwise
        // return false (the time trigger in `pipeline.rs` will flush later).
        // Remember: you may only ACK the broker AFTER a successful flush — that
        // ack-after-write ordering is what makes delivery at-least-once.
        let _ = (&self.batch_max_rows, &mut self.buffer, rows);
        todo!("V3: buffer rollups; flush on the size trigger")
    }

    /// Write the buffered rollups to ClickHouse and clear the buffer.
    pub async fn flush(&mut self) -> Result<(), AppError> {
        // TODO(V3): the batched insert — the heart of V3.
        //   - if the buffer is empty, no-op.
        //   - build ONE insert for the whole buffer (the `clickhouse` crate's
        //     `client.insert::<Row>(&table)?` + `write` per row + `end().await`,
        //     where your row type derives `clickhouse::Row` + `Serialize`). One
        //     round-trip for the batch, never one per row.
        //   - on success, clear the buffer. On error, KEEP the buffer (don't ack)
        //     so redelivery retries — that's at-least-once.
        //   - IDEMPOTENCY: the table must collapse duplicate `(series_id,
        //     window_start)` rows on merge (ReplacingMergeTree) so a replayed
        //     batch doesn't double-count. See `migrations/0001_init.sql`.
        let _ = (&self.client, &self.table, &mut self.buffer);
        todo!("V3: batch-insert the buffer into ClickHouse, then clear it")
    }

    /// The configured time-based flush interval, read by the pipeline loop.
    pub fn max_delay(&self) -> Duration {
        self.batch_max_delay
    }

    /// Rows currently buffered — export as the batch-fill gauge.
    pub fn pending(&self) -> usize {
        self.buffer.len()
    }
}

/// Read historical rollups back for `GET /query` (the dashboard's initial paint,
/// before the SSE stream takes over).
///
/// A free function, not a [`Sink`] method, because the read path runs in the HTTP
/// handlers with its own ClickHouse handle — independent of the pipeline-owned
/// writer (which lives in the consumer task).
pub async fn query_range(
    client: &clickhouse::Client,
    table: &str,
    series_id: u64,
    from: chrono::DateTime<chrono::Utc>,
    to: chrono::DateTime<chrono::Utc>,
) -> Result<Vec<RollupRow>, AppError> {
    // TODO(V3, read path): SELECT rollups for `series_id` in [from, to) ordered by
    // window_start. With a ReplacingMergeTree, use FINAL (or GROUP BY) so you read
    // the deduped view, not raw duplicates.
    let _ = (client, table, series_id, from, to);
    todo!("V3: query a rollup range back out of ClickHouse")
}
