//! V4 — SSTable: the sorted, immutable on-disk file. `src/sstable.rs`.
//!
//! When a memtable (V3) fills, it is flushed to a **Sorted String Table**: a file of
//! key/value pairs written **in key order** and then never modified. Immutability is
//! what makes the whole LSM tractable — no in-place updates, no page splits, safe to
//! read without locks, safe to `mmap` or cache by block. Later writes to the same key
//! go to *newer* SSTables; a read reconciles across them by recency (V6 compaction
//! eventually collapses the duplicates).
//!
//! The file is not one flat sorted array — that would force reading the whole thing to
//! find one key. It's structured so a point lookup touches ~one block:
//!
//! ```text
//!   [ data block 0 ][ data block 1 ]…[ data block N ]   ← sorted KV pairs, ~BLOCK_SIZE each
//!   [ bloom filter                                  ]   ← V5: "is this key even here?"
//!   [ index: first-key + offset + len of each block ]   ← binary-searchable in memory
//!   [ footer: offsets of the bloom + index + magic  ]   ← fixed size, read first
//! ```
//!
//! A lookup: check the **bloom** (V5) — absent? done, skip the file. Else binary-search
//! the in-memory **index** to the one block whose key range covers the target, read
//! that block (through the **block cache**, V7), and search within it. That's the read
//! path this file exposes to the engine.
//!
//! *Concept to internalize:* why immutability + sorted order + a sparse block index buy
//! you O(log) point lookups and cheap range scans on disk, and how "flush a sorted
//! run, never edit it" turns random writes into sequential file writes.

use std::path::{Path, PathBuf};

use bytes::Bytes;

use crate::block_cache::BlockCache;
use crate::bloom::Bloom;
use crate::error::AppError;
use crate::memtable::Value;

/// One entry in the in-memory block index: the first key of a data block and where the
/// block lives in the file. Binary-searched to find the block that could hold a key.
#[derive(Debug, Clone)]
pub struct BlockHandle {
    pub first_key: Bytes,
    pub offset: u64,
    pub len: u32,
}

/// A read handle to one immutable SSTable file. Holds the parsed index + bloom in
/// memory (small); data blocks stay on disk and are pulled through the block cache.
pub struct SsTable {
    /// Stable id (also the block-cache key namespace) — e.g. the file's sequence number.
    pub id: u64,
    path: PathBuf,
    index: Vec<BlockHandle>,
    bloom: Bloom,
}

impl SsTable {
    /// Flush a sorted run of entries (a frozen memtable, V3, or a compaction output, V6)
    /// into a brand-new SSTable file at `path`, then return a read handle to it.
    ///
    /// `entries` MUST be sorted ascending by key and carry each key's winning value
    /// (a `Value::Tombstone` is written just like a value — deletes travel into the
    /// file and are only dropped by compaction).
    ///
    /// TODO(V4): write data blocks (~`block_size` bytes each), recording a
    /// [`BlockHandle`] per block; build a [`Bloom`](crate::bloom::Bloom) over all keys
    /// (V5); then append the bloom, the index, and a fixed footer pointing at both.
    /// Buffer + fsync so a crash mid-flush can't leave a half-file the WAL can't recover.
    pub fn create<'a, I>(
        path: impl AsRef<Path>,
        id: u64,
        block_size: usize,
        bloom_bits_per_key: usize,
        entries: I,
    ) -> Result<SsTable, AppError>
    where
        I: IntoIterator<Item = (&'a Bytes, &'a Value)>,
    {
        let _ = (path.as_ref(), id, block_size, bloom_bits_per_key, entries);
        todo!(
            "V4: write data blocks + a bloom (V5) + an index + a footer; fsync; return the handle"
        )
    }

    /// Open an existing SSTable file: read the footer, then load the index + bloom into
    /// memory. Data blocks are left on disk (read lazily via the cache on lookup).
    ///
    /// TODO(V4): read the fixed footer at the file tail, seek to and parse the index and
    /// the bloom (V5), and validate the magic. A bad magic / short file is
    /// [`AppError::Corrupt`], not a panic.
    pub fn open(path: impl AsRef<Path>, id: u64) -> Result<SsTable, AppError> {
        let _ = (path.as_ref(), id);
        todo!("V4: parse footer → index + bloom into memory; validate magic")
    }

    /// The file's key count / smallest / largest key drive compaction planning (V6) and
    /// `/stats`. Wired off the in-memory index (block count is a proxy until you fill in
    /// per-block key counts).
    pub fn block_count(&self) -> usize {
        self.index.len()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Point lookup: the newest value this SSTable has for `key`, or `None` if absent.
    ///
    /// A `Some(Value::Tombstone)` is a *positive* answer — it means "deleted here," and
    /// the engine's read path must stop and not consult older SSTables.
    ///
    /// TODO(V4): (1) ask the bloom (V5) — `maybe_contains(key) == false` ⇒ return `None`
    /// with zero disk I/O; (2) binary-search `self.index` for the block whose range
    /// covers `key`; (3) fetch that block through `cache` (V7) — on a miss, read it from
    /// disk, verify its CRC, and insert it; (4) search within the block for `key`.
    pub fn get(&self, key: &[u8], cache: &BlockCache) -> Result<Option<Value>, AppError> {
        let _ = (key, cache, &self.index, &self.bloom);
        todo!("V4: bloom-reject → index binary-search → block via cache → in-block search")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the file format round-trips and the read path is correct.
    //   - round-trip: `create` a table from a sorted set of KV pairs, `open` it, and
    //     `get` returns each value (and `None` for absent keys);
    //   - tombstones survive: a `Value::Tombstone` written by `create` reads back as a
    //     tombstone, not as missing;
    //   - the bloom actually skips: `get` for a key absent from the table does not read
    //     a data block (observe via a read counter / the cache miss count);
    //   - corruption: flip a byte in a data block → `get` for a key in that block reports
    //     `Corrupt` (CRC caught it), not a wrong value.
}
