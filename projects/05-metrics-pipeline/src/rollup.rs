//! V2 — The rollup engine: streaming windowed aggregation + a percentile sketch.
//!
//! Fold the point stream into per-series, per-window [`Aggregate`]s *online*
//! (single pass, bounded memory) and emit each window as a [`RollupRow`] when it
//! closes. This is the bridge between an unqueryable firehose of raw points and a
//! handful of summarized rows a dashboard can read in milliseconds.
//!
//! Two things make this more than a `HashMap` (see SPEC V2):
//!   1. **Percentiles don't average.** To answer p99 — and to roll 1m windows up
//!      into 5m/1h — you must carry a *mergeable sketch of the distribution*, not
//!      a precomputed percentile. Build one (fixed-bucket histogram or t-digest).
//!   2. **Windows must close.** Points arrive late and out of order; a
//!      **watermark** decides when a window is done and gets flushed, and what
//!      happens to a point that shows up after its window already flushed.

use std::collections::HashMap;
use std::time::Duration;

use crate::model::{Aggregate, MetricPoint, RollupRow, WindowKey};

/// Accumulates open windows and flushes them as their watermark passes.
///
/// Holds one [`Aggregate`] per `(series, window)` currently open. The size of
/// this map *is* your live memory footprint — it's bounded by active cardinality
/// × open windows, and watching it is the OOM canary (SPEC: observability).
pub struct Rollup {
    /// Tumbling-window width. Every point snaps to a multiple of this.
    window: Duration,
    /// How long to wait past a window's end before flushing it, to absorb
    /// late/out-of-order points (the watermark grace period).
    grace: Duration,
    /// Open windows keyed by `(series_id, window_start)`.
    open: HashMap<WindowKey, Aggregate>,
}

impl Rollup {
    pub fn new(window: Duration, grace: Duration) -> Self {
        Self {
            window,
            grace,
            open: HashMap::new(),
        }
    }

    /// Fold one point into its window's running aggregate.
    pub fn ingest(&mut self, point: &MetricPoint) {
        // TODO(V2): the online aggregation step.
        //   - snap `point.timestamp` DOWN to a multiple of `self.window` to get
        //     `window_start`; build the `WindowKey` (needs the series fingerprint
        //     from V1).
        //   - upsert the `Aggregate`: bump count, add to sum, min/max, set last,
        //     and FEED THE VALUE INTO THE SKETCH. Never keep the raw values.
        //   - if the point is older than the current watermark (its window has
        //     already flushed), it's LATE: drop it and count it, or re-open —
        //     your policy (SPEC V2). Don't silently grow the map forever.
        let _ = (&self.window, &mut self.open, point);
        todo!("V2: snap to window + update the online aggregate (incl. the sketch)")
    }

    /// Flush every window whose watermark has passed (`window_end + grace < now`),
    /// removing it from the open set and returning it as a finished row.
    ///
    /// Called on a timer by the pipeline loop; the returned rows go to the sink
    /// (V3) and the SSE fan-out (V4).
    pub fn flush_ready(&mut self, now: chrono::DateTime<chrono::Utc>) -> Vec<RollupRow> {
        // TODO(V2): the watermark flush.
        //   - find keys whose `window_start + window + grace <= now`.
        //   - for each, remove it and turn its `Aggregate` into a `RollupRow`,
        //     querying the sketch for p50/p99.
        //   - returning them here is what bounds memory: a window that flushed is
        //     gone from `self.open`.
        let _ = (&self.grace, &mut self.open, now);
        todo!("V2: flush windows whose watermark has passed into RollupRows")
    }

    /// Drain *all* open windows regardless of watermark — for graceful shutdown,
    /// so a clean stop flushes partial windows instead of dropping them.
    pub fn drain_all(&mut self) -> Vec<RollupRow> {
        // TODO(V2): emit every remaining window as a row and clear the map. (Same
        // Aggregate -> RollupRow conversion as `flush_ready`; factor it out.)
        let _ = &mut self.open;
        todo!("V2: drain all open windows (used on graceful shutdown)")
    }

    /// Number of windows currently held in memory — export this as a gauge.
    pub fn open_windows(&self) -> usize {
        self.open.len()
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): the engine is pure — test it without a broker or store:
    //   - points within one window aggregate to the right count/sum/min/max;
    //   - points straddling a window boundary land in two distinct windows;
    //   - a window only appears from `flush_ready` once its watermark passes;
    //   - a late point (older than the watermark) is handled by your policy;
    //   - the SKETCH: feed a known distribution and assert the reported quantile
    //     is within your error bound of the exact percentile; assert two merged
    //     sketches answer the combined distribution (the mergeability property).
}
