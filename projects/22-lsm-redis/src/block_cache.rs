//! V7 — Block cache: a hand-built LRU over decoded SSTable blocks. `src/block_cache.rs`.
//!
//! An SSTable (V4) stores its data as fixed-ish **blocks** (a few KiB each). A point
//! read locates the one block that could hold the key, reads it from disk, and searches
//! within it. Under a skewed workload (a hot working set — Zipfian, like every real
//! cache) the *same* blocks are read over and over. Reading and decoding them from disk
//! every time is the read-amplification tax the LSM shape imposes; the **block cache**
//! is how you pay it once.
//!
//! It's a bounded, in-memory map from block identity → decoded block, with **LRU**
//! eviction: on a hit, mark the block most-recently-used; on an insert past the byte
//! budget, evict least-recently-used blocks until you fit. This is the one cache you
//! build *by hand* (no `cargo add lru`) because doing so is the point — an intrusive
//! `HashMap` + doubly-linked list (or a map + a recency queue) that stays O(1) per op.
//! Bound it by **bytes**, not entries: blocks vary in size and the budget is memory.
//!
//! Concurrency: many connections read at once, so the state is behind a lock (or shard
//! it to cut contention — note which in `docs/22-design.md`). Blocks are handed out as
//! `Arc<Block>` so a reader holds one cheaply while others touch the cache.
//!
//! *Concept to internalize:* how a block cache bounds an LSM's read amplification, why
//! LRU approximates "keep the working set" (recency ≈ reuse), and the classic O(1) LRU
//! data structure — plus how bounding by bytes (not count) matches a real memory budget.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use bytes::Bytes;

/// Identifies one block within the whole store: which SSTable, and its byte offset
/// inside that file. Cheap to hash/compare — the cache key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct BlockKey {
    pub sstable_id: u64,
    pub offset: u64,
}

/// A decoded data block, ready to search. Kept as raw bytes here; a real impl might
/// cache the parsed key/value offsets so a hit skips re-parsing too.
pub type Block = Bytes;

/// State guarded by the lock. Split out so the V7 TODO is about the *algorithm*
/// (recency + eviction), not the locking.
#[derive(Default)]
struct Inner {
    map: HashMap<BlockKey, Arc<Block>>,
    /// Sum of cached block sizes in bytes — kept `<= cap_bytes`.
    used_bytes: usize,
    // TODO(V7): add whatever recency bookkeeping your LRU needs (e.g. a doubly-linked
    // list of keys, or a monotonic access counter per entry). This is the part that
    // makes eviction O(1) and "least-recently-used" actually true.
    hits: u64,
    misses: u64,
}

/// A bounded LRU cache of decoded SSTable blocks. `new`/`stats` are wired; the get /
/// insert / eviction algorithm is V7.
pub struct BlockCache {
    cap_bytes: usize,
    inner: Mutex<Inner>,
}

impl BlockCache {
    /// A cache holding at most `cap_bytes` of blocks. `cap_bytes == 0` disables caching
    /// (every `get` misses, every `insert` is a no-op) — the read path then goes
    /// straight to disk, which is the default until you build this.
    pub fn new(cap_bytes: usize) -> Self {
        BlockCache {
            cap_bytes,
            inner: Mutex::new(Inner::default()),
        }
    }

    pub fn capacity_bytes(&self) -> usize {
        self.cap_bytes
    }

    /// `(hits, misses)` since start — the source of the block-cache hit-ratio metric the
    /// boss fight grades. Wired.
    pub fn stats(&self) -> (u64, u64) {
        let g = self.inner.lock().expect("block cache lock poisoned");
        (g.hits, g.misses)
    }

    /// Look up a block, counting the hit/miss and (on a hit) marking it most-recently-used.
    ///
    /// TODO(V7): on a hit, bump recency and return the `Arc<Block>` clone; on a miss,
    /// record it and return `None`. When `cap_bytes == 0`, always miss. Recording
    /// hits/misses here is what feeds the hit-ratio metric.
    pub fn get(&self, key: &BlockKey) -> Option<Arc<Block>> {
        let _ = key;
        todo!("V7: LRU lookup — on hit bump recency + count hit; else count miss → None")
    }

    /// Insert a freshly-read block, evicting least-recently-used blocks to stay within
    /// the byte budget.
    ///
    /// TODO(V7): insert the block, add its size to `used_bytes`, and while
    /// `used_bytes > cap_bytes` evict the LRU block (subtracting its size) until it
    /// fits. A single block larger than `cap_bytes` should simply not be cached rather
    /// than evict everything. No-op when `cap_bytes == 0`.
    pub fn insert(&self, key: BlockKey, block: Arc<Block>) {
        let _ = (key, block);
        todo!("V7: insert + mark MRU, then evict LRU blocks until used_bytes <= cap_bytes")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V7): prove bounded, correct LRU.
    //   - byte bound: after many inserts, `used_bytes` never exceeds `cap_bytes`;
    //   - LRU order: fill to capacity, touch block A, insert one more → the evicted
    //     block is the least-recently-used, NOT A;
    //   - hit accounting: a get after insert is a hit; a get for an absent key is a
    //     miss; the ratio matches the sequence you drove;
    //   - `cap_bytes == 0` disables cleanly: get always misses, insert never grows.
}
