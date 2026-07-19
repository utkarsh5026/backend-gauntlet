//! V2 — Sliding window, built from scratch.
//!
//! Motivation: a *fixed*-window counter ("≤ limit per window, reset on the
//! boundary") permits a 2× burst straddling the boundary — `limit` requests in
//! the last instant of one window and `limit` more in the first instant of the
//! next. A sliding window removes that artifact.
//!
//! Two classic implementations, with a real tradeoff:
//!   - **Sliding window log**: keep every request timestamp, evict those older
//!     than `window`, count the rest. Exact, but memory grows with traffic.
//!   - **Sliding window counter** (implemented here): keep the current and
//!     previous fixed-window counts and weight the previous one by how much of it
//!     still overlaps `now`. O(1) memory, slightly approximate.

use std::time::{Duration, Instant};

use crate::limiter::{Decision, LimitConfig};

/// A sliding-window-counter limiter for a single key.
pub struct SlidingWindowCounter {
    /// Window length (e.g. 1s derived from `burst` / `rate_per_sec`).
    window: Duration,
    /// Max requests allowed within any `window`-length span.
    limit: u64,
    /// Start of the current fixed window.
    current_start: Instant,
    /// Count accrued in the current fixed window.
    current_count: u64,
    /// Count from the immediately previous fixed window (for the weighting).
    previous_count: u64,
}

impl SlidingWindowCounter {
    pub fn new(cfg: LimitConfig, now: Instant) -> Self {
        // Derive a window from the configured rate: `limit` events per `window`.
        let window = if cfg.rate_per_sec > 0.0 {
            Duration::from_secs_f64(cfg.burst as f64 / cfg.rate_per_sec)
        } else {
            Duration::from_secs(1)
        };
        Self {
            window,
            limit: cfg.burst,
            current_start: now,
            current_count: 0,
            previous_count: 0,
        }
    }

    /// Account for a request costing `cost`, as of `now`.
    pub fn try_acquire(&mut self, cost: u64, now: Instant) -> Decision {
        // TODO(V2): implement the sliding-window-counter decision.
        //   1. Roll windows forward: if `now` has crossed into a new fixed
        //      window, shift current → previous (or zero previous if a full
        //      window was skipped) and reset `current_count`.
        //   2. Estimate the sliding count:
        //        weight   = fraction of the previous window still overlapping now
        //        estimate = previous_count * weight + current_count
        //   3. If `estimate + cost <= limit`: add `cost`, allow. Else deny with a
        //      `retry_after` derived from when enough of the window will roll off.
        let _ = (
            cost,
            now,
            self.window,
            self.limit,
            self.current_start,
            self.current_count,
            self.previous_count,
        );
        todo!("V2: sliding-window-counter decision")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): the headline test is the boundary-burst one — show that a *fixed*
    // window would allow 2×limit across a boundary, and that this counter does
    // NOT. Also: steady-state rate is respected, and `previous_count` is zeroed
    // when an entire window is skipped.
}
