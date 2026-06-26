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

use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

/// A custom epoch keeps ids small for longer. Set this to roughly "now" when you
/// start the project (ms since Unix epoch). Example below ≈ 2024-01-01.
pub const CUSTOM_EPOCH_MS: u64 = 1_704_067_200_000;

pub struct IdGenerator {
    node_id: u16,
    state: AtomicU64,
}

/// A mask to extract the 12-bit sequence portion from the packed state.
/// 0xfff equals 4095 (12 bits of 1s).
const SEQUENCE_MASK: u64 = 0xfff;

/// The maximum number of unique IDs that can be generated per node, per millisecond.
/// After reaching this value, we roll over to the next millisecond.
/// 4096 = 2^12 (the number of possible values for a 12-bit field)
const MAX_SEQUENCE: u64 = 4096;

impl IdGenerator {
    pub fn new(node_id: u16) -> Self {
        assert!(node_id < 1024, "node_id must fit in 10 bits (0..=1023)");
        Self {
            node_id,
            state: AtomicU64::new(0),
        }
    }

    /// Returns the next unique 64-bit id.
    ///
    /// Layout: `(timestamp << 22) | (node_id << 12) | sequence`.
    /// `_state` holds the last `(timestamp, sequence)` pair; a CAS loop ensures
    /// correct increments under concurrency and spins when the 12-bit sequence
    /// overflows within the same millisecond.
    pub fn next_id(&self) -> i64 {
        loop {
            let state = self.state.load(Ordering::Acquire);
            let (last_timestamp, mut seq) = {
                let last_timestamp = state >> 12;
                let sequence = state & SEQUENCE_MASK;
                (last_timestamp, sequence)
            };

            let mut current_timestamp = Self::current_timestamp_ms();

            if current_timestamp < last_timestamp {
                // Another thread may have advanced `_state` into the next ms while
                // this one still reads the previous wall-clock ms. Retry once caught up.
                std::hint::spin_loop();
                continue;
            }

            if current_timestamp == last_timestamp {
                seq += 1;
                if seq >= MAX_SEQUENCE {
                    while current_timestamp <= last_timestamp {
                        current_timestamp = Self::current_timestamp_ms();
                    }
                    seq = 0;
                }
            } else {
                seq = 0;
            }

            let new_state = (current_timestamp << 12) | seq;
            if self
                .state
                .compare_exchange_weak(state, new_state, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
            {
                let id = Self::assemble_id(current_timestamp, seq, self.node_id);
                return id as i64;
            }
        }
    }

    /// Returns the next unique ID as a Base62-encoded string ("slug").
    ///
    /// This generates a unique 64-bit ID and encodes it into a URL-friendly Base62 string.
    /// The output slug contains only 0-9, A-Z, and a-z characters and is compact for use in URLs.
    pub fn next_slug(&self) -> String {
        self.next_id_and_slug().1
    }

    /// A fresh id together with its base62 slug, from a *single* generated id.
    /// `create_link` needs both: the id is the row primary key, the slug is its
    /// public short code (the same underlying number, base62-encoded).
    pub fn next_id_and_slug(&self) -> (i64, String) {
        let id = self.next_id();
        (id, Self::base62_encode(id as u64))
    }

    fn current_timestamp_ms() -> u64 {
        (SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before Unix epoch")
            .as_millis() as u64)
            .saturating_sub(CUSTOM_EPOCH_MS)
    }

    fn assemble_id(timestamp: u64, sequence: u64, node_id: u16) -> u64 {
        (timestamp << 22) | ((node_id as u64) << 12) | sequence
    }

    fn base62_encode(n: u64) -> String {
        let mut result = String::new();
        let mut n = n;
        while n > 0 {
            result.push(BASE62_CHARS[(n % 62) as usize] as char);
            n /= 62;
        }

        // Digits are pushed least-significant first; reverse in place so we don't
        // pay for a second String (e.g. `result.chars().rev().collect()`).
        // SAFETY: only ASCII bytes from BASE62_CHARS were pushed — always valid UTF-8.
        unsafe {
            result.as_mut_vec().reverse();
        }
        result
    }
}

/// Base62 alphabet: digits, then uppercase, then lowercase.
const BASE62_CHARS: &[u8; 62] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::collections::HashSet;
    use std::sync::Arc;
    use std::thread;

    fn base62_decode(s: &str) -> Option<u64> {
        let mut n = 0u64;
        for c in s.chars() {
            let digit = BASE62_CHARS.iter().position(|&b| b as char == c)? as u64;
            n = n.checked_mul(62)?.checked_add(digit)?;
        }
        Some(n)
    }

    proptest! {
        #[test]
        fn prop_base62_roundtrip(n in 1u64..) {
            let encoded = IdGenerator::base62_encode(n);
            prop_assert!(!encoded.is_empty());
            prop_assert!(encoded.chars().all(|c| BASE62_CHARS.contains(&(c as u8))));
            prop_assert_eq!(base62_decode(&encoded), Some(n));
        }

        #[test]
        fn prop_assemble_id_preserves_fields(
            timestamp in 0u64..(1u64 << 41),
            sequence in 0u64..MAX_SEQUENCE,
            node_id in 0u16..1024u16,
        ) {
            let id = IdGenerator::assemble_id(timestamp, sequence, node_id);
            prop_assert_eq!(id >> 22, timestamp);
            prop_assert_eq!((id >> 12) & 0x3ff, node_id as u64);
            prop_assert_eq!(id & SEQUENCE_MASK, sequence);
        }

        #[test]
        fn prop_different_valid_node_ids_never_share_an_id(
            node_a in 0u16..512u16,
            node_b in 512u16..1024u16,
            count in 1usize..=200usize,
        ) {
            let g_a = IdGenerator::new(node_a);
            let g_b = IdGenerator::new(node_b);
            let ids_a: HashSet<i64> = (0..count).map(|_| g_a.next_id()).collect();
            let ids_b: HashSet<i64> = (0..count).map(|_| g_b.next_id()).collect();
            prop_assert!(ids_a.is_disjoint(&ids_b));
        }

        #[test]
        fn prop_sequential_ids_strictly_increase(
            node_id in 0u16..1024u16,
            count in 2usize..=2_000usize,
        ) {
            let gen = IdGenerator::new(node_id);
            let mut prev = gen.next_id();
            for _ in 1..count {
                let id = gen.next_id();
                prop_assert!(id > prev, "expected {id} > {prev}");
                prev = id;
            }
        }

        #[test]
        fn prop_sequential_ids_are_unique(
            node_id in 0u16..1024u16,
            count in 1usize..=2_000usize,
        ) {
            let gen = IdGenerator::new(node_id);
            let ids: HashSet<i64> = (0..count).map(|_| gen.next_id()).collect();
            prop_assert_eq!(ids.len(), count);
        }

        #[test]
        fn prop_concurrent_ids_are_unique(
            threads in 1usize..=16usize,
            ids_per_thread in 1usize..=300usize,
        ) {
            let gen = Arc::new(IdGenerator::new(7));
            let handles: Vec<_> = (0..threads)
                .map(|_| {
                    let g = Arc::clone(&gen);
                    thread::spawn(move || {
                        (0..ids_per_thread).map(|_| g.next_id()).collect::<Vec<_>>()
                    })
                })
                .collect();

            let mut all = HashSet::new();
            for handle in handles {
                for id in handle.join().expect("thread panicked") {
                    prop_assert!(all.insert(id), "duplicate id: {id}");
                }
            }
            prop_assert_eq!(all.len(), threads * ids_per_thread);
        }
    }

    #[test]
    fn base62_encode_known_values() {
        assert_eq!(IdGenerator::base62_encode(1), "1");
        assert_eq!(IdGenerator::base62_encode(10), "A");
        assert_eq!(IdGenerator::base62_encode(36), "a");
        assert_eq!(IdGenerator::base62_encode(61), "z");
        assert_eq!(IdGenerator::base62_encode(62), "10");
        assert_eq!(IdGenerator::base62_encode(3_844), "100"); // 62^2
    }

    #[test]
    fn base62_slug_uses_only_valid_chars() {
        let slug = IdGenerator::new(0).next_slug();
        assert!(!slug.is_empty());
        assert!(slug.chars().all(|c| BASE62_CHARS.contains(&(c as u8))));
    }

    #[test]
    fn ids_are_monotonically_increasing() {
        let gen = IdGenerator::new(42);
        let mut prev = gen.next_id();
        for _ in 0..1_000 {
            let id = gen.next_id();
            assert!(
                id > prev,
                "expected strictly increasing ids, got {id} after {prev}"
            );
            prev = id;
        }
    }

    #[test]
    fn ids_embed_node_id() {
        let node_id = 123;
        let gen = IdGenerator::new(node_id);
        for _ in 0..50 {
            let id = gen.next_id() as u64;
            let embedded = (id >> 12) & 0x3ff;
            assert_eq!(embedded, node_id as u64);
        }
    }

    #[test]
    fn different_node_ids_never_collide() {
        let g1 = IdGenerator::new(1);
        let g2 = IdGenerator::new(2);
        let ids1: HashSet<i64> = (0..100).map(|_| g1.next_id()).collect();
        let ids2: HashSet<i64> = (0..100).map(|_| g2.next_id()).collect();
        assert!(
            ids1.is_disjoint(&ids2),
            "generators with different node_ids shared an id"
        );
    }

    #[test]
    fn concurrent_calls_produce_unique_ids() {
        const THREADS: usize = 16;
        const IDS_PER_THREAD: usize = 500;

        let gen = Arc::new(IdGenerator::new(7));
        let handles: Vec<_> = (0..THREADS)
            .map(|_| {
                let g = Arc::clone(&gen);
                thread::spawn(move || (0..IDS_PER_THREAD).map(|_| g.next_id()).collect::<Vec<_>>())
            })
            .collect();

        let mut all = HashSet::new();
        for handle in handles {
            for id in handle.join().expect("thread panicked") {
                assert!(all.insert(id), "duplicate id under concurrency: {id}");
            }
        }
        assert_eq!(all.len(), THREADS * IDS_PER_THREAD);
    }

    #[test]
    #[should_panic(expected = "node_id must fit in 10 bits")]
    fn rejects_invalid_node_id() {
        let _ = IdGenerator::new(1024);
    }
}
