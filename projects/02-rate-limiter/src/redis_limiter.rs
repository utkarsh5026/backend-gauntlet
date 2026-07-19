//! V3 — Distributed limiter: shared state in Redis, atomic via Lua.
//!
//! V1/V2 are correct for one process. The moment you run **N** gateway instances
//! behind a load balancer, an in-memory bucket per instance enforces `N ×` the
//! intended limit. The fix is to put the state in a shared store (Redis) — but
//! naively that reintroduces a race:
//!
//! ```text
//!   instance A: GET count -> 99      instance B: GET count -> 99
//!   instance A: 99 < 100, allow      instance B: 99 < 100, allow
//!   instance A: SET 100              instance B: SET 100   // both admitted!
//! ```
//!
//! That read-modify-write must be **atomic**. A Lua script runs atomically inside
//! Redis: do the refill math and the deduct server-side, return the decision in
//! one round-trip. Load it once with `SCRIPT LOAD`, then call it by SHA with
//! `EVALSHA` (falling back to `EVAL` on `NOSCRIPT`).

use redis::aio::ConnectionManager;

use crate::error::AppError;
use crate::limiter::{Algorithm, Decision, LimitConfig};

/// Atomic token-bucket update, in Lua, executed inside Redis.
///
/// TODO(V3): write this. Rough shape of a token-bucket script:
///   KEYS[1] = bucket key
///   ARGV    = capacity, refill_per_sec, now_ms, cost, ttl
///   - read stored {tokens, last_refill} (or start full),
///   - refill = min(capacity, tokens + (now-last)/1000 * refill_per_sec),
///   - if refill >= cost: tokens = refill - cost; allowed = 1 else allowed = 0,
///   - HSET the new state, PEXPIRE the key (ttl), return {allowed, remaining, retry_ms}.
/// Keep ALL arithmetic in the script — that's what makes it race-free.
const BUCKET_LUA: &str = r#"
-- TODO(V3): atomic token-bucket refill + acquire.
return redis.error_reply("not implemented")
"#;

/// The production limiter. Cheap to clone (the connection manager is `Arc` inside).
#[derive(Clone)]
pub struct RedisLimiter {
    conn: ConnectionManager,
    cfg: LimitConfig,
    algorithm: Algorithm,
    /// When Redis is unreachable: `true` allows (fail open), `false` denies.
    fail_open: bool,
}

impl RedisLimiter {
    pub fn new(
        conn: ConnectionManager,
        cfg: LimitConfig,
        algorithm: Algorithm,
        fail_open: bool,
    ) -> Self {
        Self {
            conn,
            cfg,
            algorithm,
            fail_open,
        }
    }

    /// Atomically account for a request costing `cost` against `key`.
    pub async fn check(&self, key: &str, cost: u64) -> Result<Decision, AppError> {
        if key.is_empty() {
            return Err(AppError::InvalidArgument("key must not be empty".into()));
        }
        // TODO(V3): run the atomic update in Redis.
        //   1. Pick the script for `self.algorithm` (token bucket here; a
        //      sliding-window variant for `Algorithm::SlidingWindow`).
        //   2. `EVALSHA` it (load once, cache the SHA); on `NOSCRIPT`, `EVAL`
        //      then retry by SHA.
        //   3. Decode the returned {allowed, remaining, retry_ms} into a Decision.
        //   4. On a Redis transport error, honor `self.fail_open`: allow or deny
        //      instead of propagating — and log it.
        let _ = (cost, BUCKET_LUA, &self.conn, self.cfg, self.fail_open);
        todo!("V3: atomic Redis+Lua rate-limit check")
    }

    /// Report current state for `key` WITHOUT consuming budget.
    pub async fn peek(&self, key: &str) -> Result<Decision, AppError> {
        if key.is_empty() {
            return Err(AppError::InvalidArgument("key must not be empty".into()));
        }
        // TODO(V3): read the stored bucket state and report remaining budget
        // without mutating it (a read-only EVAL, or HGETALL + the refill math).
        todo!("V3: peek at Redis bucket state without consuming")
    }
}
