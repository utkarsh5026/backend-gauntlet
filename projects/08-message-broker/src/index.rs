//! V2 — The sparse offset index: turn a fetch-from-offset into a seek.
//!
//! Every segment (V1) has a companion `.index` next to its `.log`. It is a
//! **sparse** map of `(relative_offset → byte_position)`: an entry roughly every
//! `interval_bytes` of log, *not* one per record. To resolve a fetch at offset K
//! within a segment whose base offset is B:
//!   1. `lookup(K - B)` → binary-search for the largest indexed relative offset
//!      ≤ `K - B`, giving a byte position at or before K's frame;
//!   2. seek the `.log` to that position and scan forward the handful of frames
//!      up to K.
//!
//! Two properties make this pull its weight, and both are `Done when` criteria:
//!   - **Sparse:** entries ≪ records, so the whole index fits in memory cheaply.
//!   - **Rebuildable:** it is a *hint*, never the source of truth. Delete it and
//!     `rebuild_from_log` reconstructs it by scanning the segment's frames — the
//!     log alone is authoritative.
//!
//! Positions are `u32` **relative to the segment start**, which is why a segment
//! must stay under 4 GiB (see `SEGMENT_BYTES`).

use std::path::{Path, PathBuf};

use crate::error::AppError;

/// One sparse index entry: "the record at this relative offset begins at this
/// byte position within the segment's `.log`."
#[derive(Debug, Clone, Copy)]
pub struct IndexEntry {
    /// Offset relative to the segment's base offset (so it fits in u32).
    pub relative_offset: u32,
    /// Byte position of that record's frame within the `.log`.
    pub position: u32,
}

/// A segment's sparse index. Backed by a `.index` file; the entries are also held
/// in memory (sorted by `relative_offset`) for binary search on the read path.
pub struct Index {
    path: PathBuf,
    interval_bytes: u64,
    /// Entries in ascending `relative_offset` order.
    entries: Vec<IndexEntry>,
    /// Bytes of log written since the last index entry — drives the "every
    /// `interval_bytes`" sparsity decision on append.
    bytes_since_last: u64,
}

impl Index {
    /// Create a fresh, empty index file.
    pub fn create(path: PathBuf, interval_bytes: u64) -> std::io::Result<Self> {
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)?;
        Ok(Self {
            path,
            interval_bytes,
            entries: Vec::new(),
            bytes_since_last: 0,
        })
    }

    /// Open an existing index, loading its entries into memory.
    pub fn open(path: PathBuf, interval_bytes: u64) -> std::io::Result<Self> {
        // TODO(V2): read the `.index` file's `(relative_offset, position)` pairs
        // into `entries`. If the file is missing/short, leave it empty — the log
        // can `rebuild_from_log`. Plumbing returns an empty index for now.
        Ok(Self {
            path,
            interval_bytes,
            entries: Vec::new(),
            bytes_since_last: 0,
        })
    }

    /// Called by the log's append path (V1) after writing a frame. Adds an index
    /// entry only once ~`interval_bytes` have accrued since the last one — that's
    /// what keeps the index sparse.
    pub fn maybe_index(
        &mut self,
        relative_offset: u32,
        position: u32,
        frame_len: u64,
    ) -> Result<(), AppError> {
        // TODO(V2): accumulate `frame_len` into `bytes_since_last`; when it
        // reaches `interval_bytes`, append (relative_offset, position) to both the
        // in-memory `entries` and the `.index` file, then reset the counter.
        let _ = (relative_offset, position, frame_len, self.interval_bytes);
        todo!("V2: append a sparse index entry once interval_bytes have passed")
    }

    /// Find the byte position to start scanning from for `relative_offset`: the
    /// position of the largest indexed entry whose offset is ≤ the target. Returns
    /// 0 (segment start) when no entry precedes it.
    pub fn lookup(&self, relative_offset: u32) -> u32 {
        // TODO(V2): binary-search `entries` for the greatest `relative_offset` ≤
        // the target and return its `position`; if none, return 0. This bounds
        // the forward scan to at most one `interval_bytes` of log.
        let _ = (relative_offset, &self.entries);
        todo!("V2: binary-search the sparse index for a start position")
    }

    /// Reconstruct the index by scanning the segment's `.log` from the start —
    /// proving the index is a rebuildable hint, not the source of truth.
    pub fn rebuild_from_log(&mut self, log_path: &Path) -> Result<(), AppError> {
        // TODO(V2): clear `entries`, walk every frame in `log_path` tracking the
        // running byte position + relative offset, and re-emit a sparse entry
        // every `interval_bytes`. Rewrite the `.index` file to match.
        let _ = (log_path, &self.path);
        todo!("V2: rebuild the sparse index from the log alone")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the index —
    //   - a fetch at a mid-segment offset scans ≤ interval_bytes to find it
    //     (instrument bytes read), never the whole segment;
    //   - `entries.len()` grows ~ log_bytes / interval_bytes, i.e. it stays sparse;
    //   - deleting the `.index` and calling `rebuild_from_log` restores identical
    //     lookups.
}
