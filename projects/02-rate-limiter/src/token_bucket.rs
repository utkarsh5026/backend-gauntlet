//! V1 — Token bucket, built from scratch (in-process, single node).
//!
//! This is the algorithm in its purest form: a pure struct with no I/O, easy to
//! unit-test exhaustively before you ever involve Redis. Get this right and V3
//! becomes "the same math, but atomic and in Lua".
//!
//! The defining trick: **refill lazily, on read.** There is no background timer
//! ticking tokens into a million buckets. Instead, every `try_acquire` computes
//! how many tokens *would have* accrued since `last_refill` from the elapsed
//! time, caps at capacity, and only then tries to spend.

use std::time::Instant;

use crate::limiter::{Decision, LimitConfig};

/// A single key's token bucket. Cheap; one per key.
pub struct TokenBucket {
    /// Bucket capacity — the most tokens you can ever hold (the burst).
    capacity: f64,
    /// Tokens added per second (the sustained rate).
    refill_per_sec: f64,
    /// Current token count. Fractional on purpose — don't round away budget.
    tokens: f64,
    /// When `tokens` was last brought up to date.
    last_refill: Instant,
}

impl TokenBucket {
    /// A fresh bucket starts **full** (allowed to burst immediately).
    pub fn new(cfg: LimitConfig) -> Self {
        Self {
            capacity: cfg.burst as f64,
            refill_per_sec: cfg.rate_per_sec,
            tokens: cfg.burst as f64,
            last_refill: Instant::now(),
        }
    }

    /// Account for a request costing `cost` tokens, as of `now`.
    ///
    /// `now` is injected (rather than read internally) so tests can drive time
    /// deterministically.
    pub fn try_acquire(&mut self, cost: u64, now: Instant) -> Decision {
        // TODO(V1): implement lazy refill + acquire.
        //   1. Refill: add `elapsed.as_secs_f64() * refill_per_sec` tokens,
        //      clamped to `capacity`, then advance `last_refill` to `now`.
        //   2. If `tokens >= cost`: deduct and return `Decision::allow(...)`.
        //   3. Else: compute how long until `cost` tokens exist and return
        //      `Decision::deny(retry_after, capacity)`.
        // Mind the precision: keep `tokens` fractional; never let rounding
        // manufacture or destroy budget across many calls.
        let _ = (
            cost,
            now,
            self.capacity,
            self.refill_per_sec,
            self.tokens,
            self.last_refill,
        );
        todo!("V1: lazy token-bucket refill + acquire")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the algorithm. Suggested cases:
    //   - a full bucket allows exactly `burst` immediate requests, then denies;
    //   - after waiting `1/rate` seconds, exactly one more is allowed;
    //   - tokens never exceed capacity no matter how long you wait;
    //   - `retry_after` on a deny is truthful (waiting it out then allows).
    // Use an injected `Instant` base + `base + Duration::from_*` to control time.
}
