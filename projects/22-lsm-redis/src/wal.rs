//! V2 — Write-ahead log: durability before acknowledgement. `src/wal.rs`.
//!
//! A memtable (V3) lives in RAM. If you acknowledge a `SET` and then the process is
//! killed before that key ever reaches an SSTable on disk, the write is gone. The
//! **write-ahead log** is the fix and the oldest trick in databases: before a mutation
//! touches the memtable, append it to an on-disk log and (per policy) `fsync`. On
//! restart you **replay** the log to rebuild the memtable exactly as it was. The rule
//! is in the name — the log is written *ahead* of the change it describes.
//!
//! Two things make this more than "append to a file":
//!
//!   1. **Framing + CRC.** Each record is length-delimited and carries a CRC32 over its
//!      bytes. A crash can leave a *torn* final record (a partial write). On replay you
//!      must detect that — a bad CRC or a short tail means "stop here, the rest never
//!      committed" — and recover everything *before* it, not panic and not return junk.
//!   2. **The fsync policy.** `fsync` per write is the safe extreme but caps you at the
//!      disk's sync rate (a few thousand/sec on a spinning disk). Redis exposes exactly
//!      this dial as `appendfsync`: [`SyncPolicy::Always`] (every write), `EverySec`
//!      (batch a second's worth — the redis default), `No` (let the OS decide). The
//!      choice *is* the durability-vs-throughput tradeoff; **group commit** — one fsync
//!      amortized over many concurrently-queued writes — is how real engines cheat it.
//!
//! *Concept to internalize:* why durability means "on stable storage before the ack,"
//! what `fsync` actually guarantees (and that a `write` alone guarantees nothing after
//! a power cut), and how a CRC turns a silent torn tail into a clean truncation point.

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};

use bytes::Bytes;

use crate::error::AppError;

/// When the WAL forces its bytes to stable storage. Mirrors redis `appendfsync`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncPolicy {
    /// `fsync` after every appended record — safest, slowest.
    Always,
    /// `fsync` at most once per second (a background flush) — the redis default.
    EverySec,
    /// Never `fsync` explicitly; rely on the OS page-cache flush. Fastest, weakest.
    No,
}

/// Parse the `WAL_SYNC` env value into a policy, defaulting to `EverySec`.
pub fn parse_sync_policy(s: &str) -> SyncPolicy {
    match s.trim().to_ascii_lowercase().as_str() {
        "always" => SyncPolicy::Always,
        "no" | "never" => SyncPolicy::No,
        _ => SyncPolicy::EverySec,
    }
}

/// The mutation kind a WAL record (and later a memtable entry / SSTable row) carries.
/// A delete is *not* a file truncation — it's a logically-appended **tombstone** that
/// shadows older values until compaction (V6) finally drops it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    Set,
    Delete,
}

/// One logical mutation, as it is logged and replayed. `seq` is the monotonic sequence
/// number that orders writes across the whole engine (memtable + every SSTable) so the
/// *newest* value for a key always wins a read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalRecord {
    pub seq: u64,
    pub op: Op,
    pub key: Bytes,
    /// `None` for a `Delete` (a tombstone carries no value).
    pub value: Option<Bytes>,
}

/// An append-only durable log. Wiring (open/create the file for append, hold the path
/// and policy) is done; the framing, the fsync discipline, and replay are V2.
pub struct Wal {
    file: File,
    path: PathBuf,
    policy: SyncPolicy,
}

impl Wal {
    /// Open (creating if absent) the log file for appending. This is plumbing, not the
    /// vertical — the interesting parts are [`append`](Wal::append) and [`replay`].
    pub fn open(path: impl AsRef<Path>, policy: SyncPolicy) -> Result<Wal, AppError> {
        let path = path.as_ref().to_path_buf();
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        Ok(Wal { file, path, policy })
    }

    /// The current on-disk size in bytes. `main` uses this to decide whether there is
    /// anything to replay on startup (a fresh, empty log needs none).
    pub fn len_bytes(&self) -> Result<u64, AppError> {
        Ok(self.file.metadata()?.len())
    }

    /// Append one record durably, honoring the [`SyncPolicy`].
    ///
    /// TODO(V2): frame `rec` (a length prefix + `seq`/`op`/`key`/`value` + a CRC32 over
    /// the frame), write it, and — for [`SyncPolicy::Always`] — `fsync` before returning
    /// so the caller may only ack *after* this resolves. For `EverySec`, a separate
    /// timer flushes; this call just writes. Real engines batch many queued appends into
    /// one fsync here (group commit) — note your choice in `docs/22-design.md`.
    pub fn append(&mut self, rec: &WalRecord) -> Result<(), AppError> {
        let _ = rec;
        todo!("V2: frame the record with a length + CRC32, write it, honor the sync policy")
    }

    /// Force any buffered bytes to stable storage now (the `EverySec` timer tick, and
    /// the final flush on graceful shutdown). Wired on top of `fsync`.
    pub fn sync(&mut self) -> Result<(), AppError> {
        use std::io::Write as _;
        self.file.flush()?;
        self.file.sync_data()?;
        Ok(())
    }

    /// Replay every intact record from the log at `path`, in write order.
    ///
    /// TODO(V2): read frames until EOF, verifying each CRC. The moment a record is
    /// short or its CRC fails you've hit the **torn tail** from a crash mid-append —
    /// stop and return everything *before* it (that prefix is what was durably
    /// committed). A corrupt frame in the *middle* (not at the tail) is real
    /// corruption → [`AppError::Corrupt`]. Returning junk here silently resurrects a
    /// write that was never acknowledged, so this is the function durability rides on.
    pub fn replay(path: impl AsRef<Path>) -> Result<Vec<WalRecord>, AppError> {
        let _ = path.as_ref();
        todo!("V2: read + CRC-verify frames, truncating cleanly at the first torn/short tail")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove durability + torn-tail recovery.
    //   - round-trip: append N records, `replay` returns exactly those N in order
    //     (a proptest over random key/value/op sequences);
    //   - torn tail: truncate the log file mid-final-record, then `replay` returns the
    //     first N-1 and does NOT error — the partial write is dropped, not resurrected;
    //   - bit-flip a byte in a middle record → `replay` reports `Corrupt`, not silence;
    //   - `SyncPolicy::Always` calls fsync before `append` returns (observe via a
    //     fault-injecting file or a fsync counter).
}
