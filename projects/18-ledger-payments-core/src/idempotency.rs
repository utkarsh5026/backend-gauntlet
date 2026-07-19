//! V3 — Idempotency keys: exactly-once *effects* over an at-least-once network.
//!
//! A client whose `POST /transfers` times out doesn't know if the money moved, so it
//! retries. Without protection you've double-charged. The `Idempotency-Key` header
//! fixes it: the first request with a key does the work and **stores its response**;
//! any replay of that key returns the stored response without re-executing.
//!
//! Storage is two-tier: **Redis** caches the response for fast replay, **Postgres**
//! (`idempotency_keys`) is the durable record. A cache miss re-reads Postgres and
//! *still never re-executes*. The record binds the key to a **request fingerprint**,
//! so the same key with a different body is a conflict — not a silent replay.
//!
//! The hard case is the **concurrent** double-submit: two identical requests arrive
//! before either finishes. Exactly one must execute; the other must wait for / return
//! the same result. A `UNIQUE` insert (or `INSERT … ON CONFLICT`) on the key is the
//! usual lever — reserve the key first, execute second.

use std::sync::Arc;

use redis::aio::ConnectionManager;
use serde::{Deserialize, Serialize};
use sqlx::PgPool;

use crate::error::AppError;
use crate::money::TxId;

/// A stored idempotency result — what a replay returns instead of re-executing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredResponse {
    pub status_code: u16,
    pub transaction_id: Option<TxId>,
    pub body: serde_json::Value,
}

/// What a lookup found for a key. Drives the handler's branch: execute, replay, or
/// reject.
#[derive(Debug)]
pub enum KeyState {
    /// No record for this key — the caller reserved it and should execute now.
    Fresh,
    /// A completed request with this key + matching fingerprint — replay this.
    Replay(StoredResponse),
    /// This key exists but is bound to a *different* request body — a conflict.
    Mismatch,
    /// The key is reserved by an in-flight request that hasn't finished yet — the
    /// caller should wait and re-check, or return "in progress" (your policy).
    InProgress,
}

/// Two-tier idempotency store: Redis cache in front of the durable Postgres record.
pub struct IdempotencyStore {
    pool: PgPool,
    redis: ConnectionManager,
    ttl_secs: i64,
}

impl IdempotencyStore {
    pub fn new(pool: PgPool, redis: ConnectionManager, ttl_secs: i64) -> Arc<Self> {
        Arc::new(Self {
            pool,
            redis,
            ttl_secs,
        })
    }

    /// Fingerprint a request body so the same key + a different body is detectable as
    /// a conflict.
    ///
    /// TODO(V3): hash the *canonical* request bytes (e.g. SHA-256 hex). Canonical
    /// matters — two JSON bodies that differ only in key order are the "same" request,
    /// so hash a normalized form, not the raw bytes, or document that you don't.
    pub fn fingerprint(body: &[u8]) -> String {
        let _ = body;
        todo!("V3: stable fingerprint of the request body")
    }

    /// Look up (and, if fresh, *reserve*) a key. This is the concurrency-critical step.
    ///
    /// TODO(V3): the reserve-then-execute handshake.
    ///   1. Fast path: check Redis for a cached [`StoredResponse`] under the key. On a
    ///      hit whose fingerprint matches, return `Replay` (count an idempotency hit).
    ///   2. Miss: go to Postgres. Atomically try to *insert* a reservation row for the
    ///      key (`INSERT … ON CONFLICT DO NOTHING`, storing `request_hash`, an unset
    ///      response, and `expires_at = now() + ttl`):
    ///        - insert won: return `Fresh` — the caller owns execution.
    ///        - insert lost (row already there): read it.
    ///            · fingerprint differs        -> `Mismatch`
    ///            · response present + fresh    -> `Replay`
    ///            · response absent (in-flight) -> `InProgress`
    ///            · expired                     -> treat as `Fresh` (per the TTL policy).
    /// The insert is what makes two concurrent identical requests safe: exactly one
    /// wins the row.
    pub async fn lookup_or_reserve(
        &self,
        key: &str,
        fingerprint: &str,
    ) -> Result<KeyState, AppError> {
        let _ = (&self.pool, &self.redis, self.ttl_secs, key, fingerprint);
        todo!("V3: check Redis, else reserve the key in Postgres (INSERT ON CONFLICT)")
    }

    /// Persist the finished result so future replays return it — Postgres (durable)
    /// first, then populate the Redis cache.
    ///
    /// TODO(V3): UPDATE the reservation row with `status_code`, `transaction_id`,
    /// `response_body`; then SET the Redis key (with the same TTL) for the fast path.
    /// Order matters: write the durable record before the cache, so a crash between
    /// them leaves the truth intact and only costs a cache miss.
    pub async fn store(&self, key: &str, response: &StoredResponse) -> Result<(), AppError> {
        let _ = (&self.pool, &self.redis, self.ttl_secs, key, response);
        todo!("V3: persist the response to Postgres, then cache it in Redis")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove idempotency (wants a real Postgres + Redis).
    //   - same key twice: the second call returns Replay with the stored response and
    //     the ledger shows exactly ONE transaction (no second posting);
    //   - same key, different body: Mismatch (a 409/422), never a stale replay;
    //   - concurrent double-submit of the same key+body: exactly one Fresh, the other
    //     Replay/InProgress — one posting total;
    //   - a Redis flush (cache miss) still returns the stored result from Postgres and
    //     does NOT re-execute;
    //   - an expired key is treated as Fresh.
}
