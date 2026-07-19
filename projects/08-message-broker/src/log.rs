//! V1 — The segmented append-only log: one partition's durable commit log.
//!
//! This is the layer Kafka would give you. A `Log` is a directory of fixed-size
//! **segment** files. Each segment is named by the base offset it starts at
//! (`00000000000000000000.log`) and holds records framed as
//! `[len u32][crc u32][timestamp i64][key_len u32][key][value]`. Appending writes
//! a frame to the tail of the active segment and returns a monotonically
//! increasing offset; when the active segment passes `segment_bytes`, it rolls to
//! a new one whose base offset is the next offset to be assigned.
//!
//! The two traps, and the whole point of V1:
//!   1. **Durability.** `write()` returning does not mean the bytes are safe. The
//!      fsync policy (per-append vs. batched) is a deliberate throughput ↔ safety
//!      dial you choose and document — not whatever the OS happened to flush.
//!   2. **Recovery.** After a crash the active segment may end in a half-written
//!      frame. `open` must scan to the last *complete* frame, set the next offset
//!      from it, and truncate the torn tail — so a consumer never sees a partial
//!      record, and the next append lands on a clean boundary.
//!
//! Each segment pairs its `.log` with a sparse `Index` (V2, `index.rs`) used to
//! turn a fetch-from-offset into a seek instead of a scan.

use std::path::{Path, PathBuf};

use crate::error::AppError;
use crate::index::Index;
use crate::record::{Offset, Record, StoredRecord};

/// Tunables shared by every partition's log. Sourced from env in `main`.
#[derive(Debug, Clone, Copy)]
pub struct LogConfig {
    /// Roll to a new segment once the active one exceeds this many bytes.
    pub segment_bytes: u64,
    /// Write a sparse index entry roughly every this-many bytes (V2).
    pub index_interval_bytes: u64,
}

/// One segment on disk: a `.log` file of framed records plus its sparse `.index`.
/// `base_offset` is the offset of this segment's first record and is encoded in
/// both filenames.
pub struct Segment {
    pub base_offset: Offset,
    log_path: PathBuf,
    index: Index,
    // TODO(V1): you'll want the current write position / an open append handle
    // here so an append doesn't reopen + seek-to-end every time.
}

impl Segment {
    /// Create a fresh, empty segment starting at `base_offset` under `dir`.
    /// Plumbing — it lays down the two files; the framing lives in `Log`.
    pub fn create(
        dir: &Path,
        base_offset: Offset,
        index_interval_bytes: u64,
    ) -> std::io::Result<Self> {
        let stem = format!("{base_offset:020}");
        let log_path = dir.join(format!("{stem}.log"));
        let index = Index::create(dir.join(format!("{stem}.index")), index_interval_bytes)?;
        // Create the (empty) log file so it exists even before the first append.
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)?;
        Ok(Self {
            base_offset,
            log_path,
            index,
        })
    }

    /// Open an existing segment whose `.log` is at `log_path`, loading its index.
    pub fn open(log_path: PathBuf, index_interval_bytes: u64) -> std::io::Result<Self> {
        let base_offset = base_offset_of(&log_path).unwrap_or(0);
        let index_path = log_path.with_extension("index");
        let index = Index::open(index_path, index_interval_bytes)?;
        Ok(Self {
            base_offset,
            log_path,
            index,
        })
    }

    /// The sparse index for this segment (V2). Used on the read path to seek.
    pub fn index(&self) -> &Index {
        &self.index
    }

    /// The `.log` file backing this segment.
    pub fn log_path(&self) -> &Path {
        &self.log_path
    }
}

/// One partition's append-only log: an ordered list of segments plus the offset
/// to assign next. Appends target the last (active) segment; reads locate the
/// segment holding the wanted offset.
pub struct Log {
    dir: PathBuf,
    config: LogConfig,
    /// Sealed + active segments, in ascending base-offset order. The last is the
    /// active (writable) one.
    segments: Vec<Segment>,
    /// The offset the *next* appended record will get == number of records so far.
    /// V1 recovery must set this from the existing segments on `open`.
    next_offset: Offset,
}

impl Log {
    /// Open (creating if needed) the log under `dir`.
    ///
    /// Plumbing sets up the directory. The **recovery** — scanning existing
    /// segments, validating the tail, and restoring `next_offset` — is V1 work.
    pub fn open(dir: impl AsRef<Path>, config: LogConfig) -> Result<Self, AppError> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir)?;

        // TODO(V1 recovery): list `*.log` files, sort by base offset, and for the
        // active (last) one scan frames to the last *complete* record —
        // truncating any torn tail — to compute `next_offset`. For now we start
        // empty; the first append must lazily create the base segment.
        Ok(Self {
            dir,
            config,
            segments: Vec::new(),
            next_offset: 0,
        })
    }

    /// The offset that will be assigned to the next append == the log-end offset.
    /// Consumer lag (a horizontal metric) is this minus the group's committed offset.
    pub fn log_end_offset(&self) -> Offset {
        self.next_offset
    }

    /// Append a record, returning the offset it was assigned. The core of V1.
    pub async fn append(&mut self, record: &Record) -> Result<Offset, AppError> {
        // TODO(V1): the append path —
        //   - if there is no active segment, or the active one is past
        //     `config.segment_bytes`, roll: create a Segment with base_offset =
        //     next_offset and push it.
        //   - frame the record: [len][crc(of the rest)][timestamp][key_len][key][value],
        //     append it to the active segment's `.log`.
        //   - update the segment's sparse index (V2) if `index_interval_bytes`
        //     of log have passed since the last index entry.
        //   - apply the chosen fsync policy (per-append, or batched).
        //   - assign `next_offset`, increment it, return the assigned offset.
        let _ = (record, &self.config, &mut self.segments);
        todo!("V1: frame + durably append a record, rolling the segment if full")
    }

    /// Read up to `max_records` records starting at `offset`. Returns an empty
    /// vec (not an error) when `offset` is at or past the log end — the tailing
    /// consumer case.
    pub async fn read_from(
        &mut self,
        offset: Offset,
        max_records: usize,
    ) -> Result<Vec<StoredRecord>, AppError> {
        // TODO(V1 + V2): locate the segment whose base_offset is the largest ≤
        // offset (V3-style search over `segments`), then use its sparse index
        // (V2) to seek near `offset` and scan forward, decoding + CRC-checking
        // each frame, until `max_records` are collected or the log ends. A frame
        // that fails its check is `AppError::CorruptFrame`, never returned as data.
        let _ = (offset, max_records, &self.dir, &self.segments);
        todo!("V1/V2: seek to `offset` via the sparse index and read a batch of frames")
    }
}

/// Parse the base offset a segment file encodes in its name
/// (`00000000000000000042.log` → 42). Used by recovery + read to locate segments.
fn base_offset_of(path: &Path) -> Option<Offset> {
    path.file_stem()?.to_str()?.parse().ok()
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the log —
    //   - append returns 0,1,2,… and read_from round-trips the exact bytes;
    //   - reopening a log recovers next_offset and every record (restart safety);
    //   - a torn tail frame (write partial bytes, reopen) is truncated, not read;
    //   - flipping a byte in a committed frame surfaces as CorruptFrame on read;
    //   - enough appends roll a second segment file into existence.
}
