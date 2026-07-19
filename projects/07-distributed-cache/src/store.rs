//! V1 — The per-node bounded cache with O(1) eviction.
//!
//! This is the layer you'd normally get from the `lru` crate (or just lean on
//! Redis for). Here you build it: a `HashMap` for O(1) lookup, plus a *second*
//! structure that lets you find and drop the eviction victim in O(1) too. That
//! second structure is the whole game — with only a map, "evict the least
//! recently used" is an O(n) scan on every insert, which defeats the point.
//!
//! Scaffold state: the store is constructed and shared, but every real operation
//! is a `todo!()`. The first `GET`/`PUT` that reaches it panics with the todo
//! message — that panic is your worklist.
//!
//! Suggested shape (yours to decide): a `HashMap<String, Entry>` for values, and
//! for LRU an intrusive doubly-linked list threaded through the entries so a
//! `get` can splice a node to the front in O(1); for LFU a frequency index. Keep
//! the policy behind one trait so LRU and LFU are swappable (a SPEC criterion).

use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;

/// Which victim the store drops when it's full. Swappable per the SPEC so you can
/// build LRU first and add LFU without touching the store's plumbing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvictionPolicy {
    /// Evict the least *recently* used entry.
    Lru,
    /// Evict the least *frequently* used entry.
    Lfu,
}

impl std::str::FromStr for EvictionPolicy {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_ascii_lowercase().as_str() {
            "lru" => Ok(Self::Lru),
            "lfu" => Ok(Self::Lfu),
            other => Err(format!("unknown eviction policy `{other}` (want lru|lfu)")),
        }
    }
}

/// A stored value plus its expiry. `Bytes` is reference-counted, so cloning a
/// value out to a response is cheap (no copy of the payload).
#[derive(Clone, Debug)]
pub struct Entry {
    pub value: Bytes,
    /// `None` = never expires; `Some(t)` = dead once `Instant::now() >= t`.
    pub expires_at: Option<Instant>,
}

impl Entry {
    fn is_expired(&self, now: Instant) -> bool {
        matches!(self.expires_at, Some(t) if now >= t)
    }
}

/// The bounded local cache. `get`/`put` take `&self` because the store is shared
/// across all request handlers — the interior mutability (and the choice of one
/// big lock vs sharded locks) is part of the V1 design you own.
pub struct Store {
    /// Hard cap on live entries. A `put` into a full store must evict first.
    capacity: usize,
    policy: EvictionPolicy,
    // TODO(V1): your real state lives here, behind a lock (or several — sharding
    // the lock by key hash is how you avoid one global mutex becoming the
    // bottleneck under contention; that's a documented decision in the SPEC).
    //
    //   inner: Mutex<Inner>,                 // or Vec<Mutex<Inner>> for shards
    //   where Inner holds the HashMap + the recency/frequency index.
}

impl Store {
    /// Build an empty store. Plumbing — the interesting methods are yours.
    pub fn new(capacity: usize, policy: EvictionPolicy) -> Arc<Self> {
        assert!(capacity > 0, "cache capacity must be > 0");
        Arc::new(Self { capacity, policy })
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn policy(&self) -> EvictionPolicy {
        self.policy
    }

    /// Look up a key. Returns the value only if present **and** not expired.
    ///
    /// This is on the hot read path and it *mutates* bookkeeping: an LRU hit must
    /// move the entry to the most-recently-used position, an LFU hit must bump
    /// its frequency. That's why a naive `RwLock` read guard isn't enough — a
    /// `get` writes.
    pub fn get(&self, key: &str) -> Option<Bytes> {
        // TODO(V1): O(1) lookup; on hit, update recency/frequency for `policy`;
        // treat an expired entry as a miss (and drop it so it stops counting).
        let _ = (self.capacity, self.policy, key, Instant::now);
        todo!("V1: O(1) get that updates the eviction bookkeeping and honours TTL")
    }

    /// Insert or overwrite a key. If the store is at capacity and this is a new
    /// key, evict exactly one victim (per `policy`) *before* inserting.
    pub fn put(&self, key: String, value: Bytes, ttl: Option<Duration>) {
        // TODO(V1): compute expires_at from ttl (Instant::now() + ttl); insert;
        // if inserting a NEW key would exceed capacity, evict the policy's victim
        // first — O(1), no scan. Overwriting an existing key must not evict.
        let _ = (&self, key, value, ttl);
        todo!("V1: bounded put with O(1) eviction of the policy's victim")
    }

    /// Remove a key if present; returns whether it existed.
    pub fn remove(&self, key: &str) -> bool {
        // TODO(V1): remove from the map AND unlink it from the recency/frequency
        // index so the two structures never disagree.
        let _ = key;
        todo!("V1: remove from both the map and the ordering index")
    }

    /// Number of live (non-expired) entries — for the capacity invariant test and
    /// the per-node key-count metric (observability horizontal).
    pub fn len(&self) -> usize {
        todo!("V1: count of live entries")
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the store:
    //   - capacity invariant: a random sequence of puts never exceeds capacity
    //     (property test);
    //   - LRU vs LFU diverge: the same access trace makes the two policies pick
    //     *different* victims;
    //   - TTL: an entry read after its ttl is a miss and stops counting toward len;
    //   - a `get` on the hot key keeps it from being the eviction victim.
}
