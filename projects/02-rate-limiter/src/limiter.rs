//! Shared rate-limiter vocabulary.
//!
//! The three vertical challenges — [`crate::token_bucket`] (V1),
//! [`crate::sliding_window`] (V2), and [`crate::redis_limiter`] (V3) — all speak
//! in these terms: a [`LimitConfig`] in, a [`Decision`] out. Keeping the contract
//! here means you can swap algorithms without touching the gRPC layer.

use std::time::Duration;

/// Which algorithm the distributed limiter enforces. Selected via config.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Algorithm {
    TokenBucket,
    SlidingWindow,
}

impl std::str::FromStr for Algorithm {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "token_bucket" | "token-bucket" | "tokenbucket" => Ok(Self::TokenBucket),
            "sliding_window" | "sliding-window" | "slidingwindow" => Ok(Self::SlidingWindow),
            other => Err(format!("unknown algorithm `{other}`")),
        }
    }
}

/// The configured budget for a key.
#[derive(Debug, Clone, Copy)]
pub struct LimitConfig {
    /// Sustained refill rate, in tokens per second (the long-run average).
    pub rate_per_sec: f64,
    /// Maximum burst — the token-bucket capacity / sliding-window ceiling.
    pub burst: u64,
}

/// The outcome of a single rate-limit decision, returned to the caller.
#[derive(Debug, Clone, Copy)]
pub struct Decision {
    /// Whether the request is permitted.
    pub allowed: bool,
    /// Budget remaining after this decision.
    pub remaining: u64,
    /// The ceiling that applied.
    pub limit: u64,
    /// If denied, how long to wait before a retry could succeed.
    /// Zero when allowed.
    pub retry_after: Duration,
}

impl Decision {
    /// Build an "allowed" decision.
    pub fn allow(remaining: u64, limit: u64) -> Self {
        Self {
            allowed: true,
            remaining,
            limit,
            retry_after: Duration::ZERO,
        }
    }

    /// Build a "denied" decision with a truthful retry hint.
    pub fn deny(retry_after: Duration, limit: u64) -> Self {
        Self {
            allowed: false,
            remaining: 0,
            limit,
            retry_after,
        }
    }
}
