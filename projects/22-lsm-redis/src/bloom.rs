//! V5 — Bloom filters: skip the SSTables that can't hold the key. `src/bloom.rs`.
//!
//! A read for a key that isn't in the memtable has to consult SSTables newest→oldest.
//! With many SSTables per level, a *miss* is the worst case: you'd touch every file
//! only to find nothing. A **bloom filter** per SSTable turns most of that into a
//! single in-memory check. It's a bit array plus `k` hash functions; `insert` sets `k`
//! bits per key, `maybe_contains` checks those `k` bits:
//!
//!   - all `k` bits set → the key *might* be present (read the file to be sure);
//!   - any bit clear → the key is **definitely not** present (skip the file entirely).
//!
//! The one-sided error is the whole point: **no false negatives** (never say "absent"
//! about a key you inserted — that would lose data), a tunable **false-positive** rate
//! (occasionally read a file for nothing). More bits-per-key and the right `k` push the
//! FP rate down; the classic sizing is `bits ≈ -n·ln(p)/ln(2)²` and `k ≈ bits/n·ln(2)`.
//! `BLOOM_BITS_PER_KEY = 10` gives roughly a 1% FP rate — LevelDB's default.
//!
//! You don't need a crypto hash. Two `std::hash::Hasher` outputs combined
//! (`h1 + i·h2`, the Kirsch–Mitzenmacher double-hashing trick) synthesize all `k`
//! indices cheaply — building that is part of the exercise, so no hash crate is pulled.
//!
//! *Concept to internalize:* trading a little space and a tunable false-positive rate
//! for skipping disk I/O, and why the *no-false-negatives* guarantee is non-negotiable
//! in a database (a false negative silently drops a key that's really on disk).

/// A bloom filter over a fixed key set, serialized into the tail of each SSTable (V4).
pub struct Bloom {
    /// The bit array, packed 8 bits per byte.
    bits: Vec<u8>,
    /// Number of hash probes per key (`k`).
    k: u32,
}

impl Bloom {
    /// Size a filter for `expected_keys` at `bits_per_key`, choosing `k` to minimize
    /// the false-positive rate.
    ///
    /// TODO(V5): allocate `ceil(expected_keys * bits_per_key / 8)` bytes and set
    /// `k = round(bits_per_key * ln2)` (clamped to at least 1). This fixes the filter's
    /// geometry; `insert` fills it. (`expected_keys == 0` should still yield a valid,
    /// tiny filter that answers "definitely not present" for everything.)
    pub fn new(expected_keys: usize, bits_per_key: usize) -> Self {
        let _ = (expected_keys, bits_per_key);
        todo!("V5: size the bit array from bits_per_key and pick k = round(bits_per_key * ln2)")
    }

    /// Reconstruct a filter from its serialized `bits` and `k` (read back from an
    /// SSTable). Wired — the interesting sizing is in [`new`](Bloom::new).
    pub fn from_parts(bits: Vec<u8>, k: u32) -> Self {
        Bloom { bits, k }
    }

    /// The raw bit array (for writing the filter into the SSTable footer, V4).
    pub fn as_bytes(&self) -> &[u8] {
        &self.bits
    }

    /// The probe count `k` (serialized alongside the bits).
    pub fn hashes(&self) -> u32 {
        self.k
    }

    /// Record `key`'s membership: set its `k` bits.
    ///
    /// TODO(V5): derive two base hashes of `key`, then for `i in 0..k` set the bit at
    /// `(h1 + i*h2) % (bits.len()*8)`. Every key inserted here MUST later be reported
    /// present by [`maybe_contains`](Bloom::maybe_contains) — that's the no-false-negatives
    /// contract.
    pub fn insert(&mut self, key: &[u8]) {
        let _ = key;
        todo!("V5: set the k bits for `key` via double hashing")
    }

    /// Membership test: `false` = definitely absent (skip the SSTable); `true` = maybe
    /// present (read it).
    ///
    /// TODO(V5): probe the same `k` bits `insert` would set; return `false` if any is
    /// clear, `true` if all are set. Must never return `false` for a key that was
    /// inserted.
    pub fn maybe_contains(&self, key: &[u8]) -> bool {
        let _ = key;
        todo!("V5: return false only if some of `key`'s k bits are clear")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V5): prove the guarantee, then measure the tradeoff.
    //   - NO false negatives: insert a random set of keys, assert `maybe_contains` is
    //     true for every one of them (a proptest — this is the invariant that protects
    //     data);
    //   - false-positive rate: over many *absent* keys, the fraction reported present
    //     is near the theoretical p for the chosen bits_per_key (e.g. ~1% at 10);
    //   - an empty filter (`new(0, _)`) answers false for everything and never panics.
}
