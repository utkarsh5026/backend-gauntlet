//! V1 — Distributed, coordination-free ID generation (Snowflake-style).
//!
//! GOAL: generate 64-bit, time-ordered, unique ids entirely in-process — no DB
//! sequence, no network round-trip — then base62-encode them into short slugs.
//!
//! Classic Snowflake layout (you can tweak the bit budget):
//!   [ 1 unused ][ 41 bits ms since epoch ][ 10 bits node id ][ 12 bits sequence ]
//! - 41 bits of ms  → ~69 years from your chosen epoch
//! - 10 bits node   → up to 1024 instances
//! - 12 bits seq    → up to 4096 ids per node per millisecond
//!
//! Concurrency: many requests will call `next_id` at once, so the internal state
//! (last timestamp + sequence) must be updated atomically. An `AtomicU64` packing
//! both fields, updated with a CAS loop, is one clean approach.

use std::sync::atomic::AtomicU64;

/// A custom epoch keeps ids small for longer. Set this to roughly "now" when you
/// start the project (ms since Unix epoch). Example below ≈ 2024-01-01.
pub const CUSTOM_EPOCH_MS: u64 = 1_704_067_200_000;

pub struct IdGenerator {
    node_id: u16,
    /// Packs (last_timestamp, sequence) so updates are a single atomic CAS.
    /// TODO(V1): decide your exact packing and use it in `next_id`.
    _state: AtomicU64,
}

impl IdGenerator {
    /// `node_id` must be unique per running instance (0..=1023). It comes from
    /// the `NODE_ID` env var (see .env.example).
    pub fn new(node_id: u16) -> Self {
        assert!(node_id < 1024, "node_id must fit in 10 bits (0..=1023)");
        Self {
            node_id,
            _state: AtomicU64::new(0),
        }
    }

    /// Returns the next unique 64-bit id.
    ///
    /// TODO(V1):
    /// 1. Read the current ms timestamp (since `CUSTOM_EPOCH_MS`).
    /// 2. If same ms as last call → increment sequence; if sequence overflows,
    ///    spin until the next millisecond.
    /// 3. If a new ms → reset sequence to 0.
    /// 4. Handle clock-going-backwards (refuse / wait — decide and document).
    /// 5. Pack: (ts << 22) | (node_id << 12) | sequence.
    ///
    /// Do all of this with atomics so it's correct under concurrency.
    pub fn next_id(&self) -> i64 {
        let _ = self.node_id;
        todo!("V1: implement Snowflake id generation")
    }

    /// Convenience: a fresh id already base62-encoded into a slug.
    pub fn next_slug(&self) -> String {
        base62_encode(self.next_id() as u64)
    }
}

/// TODO(V1): base62 encoding ([0-9A-Za-z]). Implement encode; decode is optional
/// (you can store the slug directly). Keep it allocation-light if you can.
pub fn base62_encode(_n: u64) -> String {
    todo!("V1: implement base62 encoding")
}

#[cfg(test)]
mod tests {
    // TODO(V1): write tests proving:
    // - ids are monotonically increasing,
    // - no collisions across many concurrent calls (spawn N tasks),
    // - two generators with different node_ids never collide,
    // - base62 round-trips (if you implement decode).
}
