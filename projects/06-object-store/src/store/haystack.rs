//! Haystack-style small-object packing — From the field (scaffold).
//!
//! Many tiny CAS objects share a handful of append-only **volume** files; an
//! in-memory map locates each needle by `digest → (volume, offset, size)`.
//! See [`docs/11-how-haystack-packing-works.md`](../../docs/11-how-haystack-packing-works.md).
//!
//! This module is the **physical layout** alternative to [`super::file_cas::FileCas`].
//! Identity stays the plaintext digest — volumes are anonymous containers.
//!
//! ## On-disk needle
//!
//! ```text
//! [ digest hex: Digest::LEN bytes ][ size: u64 LE ][ payload: size bytes ]
//! ```
//!
//! [`NeedleLocator::payload_offset`] is the **payload** start (after the header)
//! so GET can seek + `take(size)` without re-parsing.
//!
//! ## Durable live index
//!
//! Mutations append one NDJSON record to `volumes/needles.log` (fsynced) and
//! update the RAM map. A background (or explicit) **checkpoint** rewrites
//! `volumes/needles.json` from RAM and truncates the log — JSON is a fast-boot
//! snapshot, the log is the durability path. Open loads JSON (if any) then
//! replays the log; volume scan is recovery only when both are missing.
//! [`Haystack::remove`] / quarantine append tombstone ops; compaction copies live
//! needles into a **new** volume id, remaps the index, then unlinks the old
//! file (closed volumes are never rewritten in place).
//!
//! ## Scaffold status
//!
//! Commit, open, durable remove, recovery rebuild, needle scrubbing, and
//! volume compaction are implemented. Default store boot stays on
//! [`BlobLayoutKind::FileCas`](super::BlobLayoutKind::FileCas);
//! select this layout with `BLOB_LAYOUT=haystack`.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom as SyncSeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};
use tokio::fs::OpenOptions;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt, Take};

use crate::durable::{atomic_write_sibling_sync, publish_temp, TempEntry};
use crate::error::AppError;
use crate::object::Digest;
use uuid::Uuid;

/// Soft volume capacity default when `HAYSTACK_MAX_VOLUME_SIZE` is unset (1 MiB).
///
/// Override at boot with a raw byte count, e.g. `HAYSTACK_MAX_VOLUME_SIZE=1073741824`.
/// Needles that do not fit fall back to [`crate::store::FileCas`] under hybrid /
/// haystack policy.
pub const DEFAULT_MAX_VOLUME_SIZE: u64 = 1024 * 1024;

/// How often the Store-spawned task checkpoints when the WAL is dirty.
pub const INDEX_CHECKPOINT_INTERVAL: Duration = Duration::from_millis(500);

const VOLUME_EXT: &str = "dat";
const INDEX_FILE: &str = "needles.json";
const LOG_FILE: &str = "needles.log";
/// Bumped when `volume_id` switched from `u32` to UUID (hyphenated string on disk).
const INDEX_VERSION: u32 = 2;
/// Hex digest (`Digest::LEN`) + little-endian `u64` payload length.
const NEEDLE_HEADER_LEN: u64 = Digest::LEN as u64 + std::mem::size_of::<u64>() as u64;

/// On-disk snapshot shape of [`INDEX_FILE`] — compacted checkpoint of the RAM map.
#[derive(Debug, Serialize, Deserialize)]
struct NeedleIndexFile {
    version: u32,
    entries: Vec<NeedleIndexEntry>,
}

/// One row in [`NeedleIndexFile`] — on-disk mirror of a [`NeedleRecord`].
///
/// The JSON field is still named `offset` (INDEX_VERSION 2); it stores the
/// payload start, matching [`NeedleLocator::payload_offset`].
#[derive(Debug, Serialize, Deserialize)]
struct NeedleIndexEntry {
    digest: Digest,
    volume_id: VolumeId,
    /// Payload byte offset (serialized as `offset`).
    offset: u64,
    size: u64,
    /// Tombstone: logical delete; compaction drops the entry and skips the bytes.
    #[serde(default)]
    deleted: bool,
    /// Scrub failure: refuse GET; compaction may drop like deleted.
    #[serde(default)]
    quarantined: bool,
}

impl NeedleIndexEntry {
    /// Build a snapshot row from a live RAM record.
    fn from_record(digest: &Digest, record: &NeedleRecord) -> Self {
        let NeedleLocator {
            volume_id,
            payload_offset,
            size,
        } = record.locator;
        Self {
            digest: digest.clone(),
            volume_id,
            // On-disk key stays `offset` (INDEX_VERSION 2); the Rust field name
            // is payload_offset on NeedleLocator.
            offset: payload_offset,
            size,
            deleted: record.deleted,
            quarantined: record.quarantined,
        }
    }
}

/// One durable mutation in `needles.log` (NDJSON, one object per line).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum IndexLogOp {
    /// Index (or replace) a live needle at `offset` (payload start).
    Put {
        digest: Digest,
        volume_id: VolumeId,
        offset: u64,
        size: u64,
    },
    /// Mark `digest` deleted; bytes stay until compaction.
    Delete { digest: Digest },
    /// Mark `digest` quarantined after a scrub mismatch.
    Quarantine { digest: Digest },
}

impl IndexLogOp {
    /// WAL `put` for a freshly committed locator.
    fn put(digest: &Digest, locator: NeedleLocator) -> Self {
        Self::Put {
            digest: digest.clone(),
            volume_id: locator.volume_id,
            offset: locator.payload_offset,
            size: locator.size,
        }
    }

    /// Replay this op onto the in-memory map (boot / recovery).
    fn apply_to(self, map: &mut HashMap<Digest, NeedleRecord>) {
        match self {
            Self::Put {
                digest,
                volume_id,
                offset,
                size,
            } => {
                map.insert(
                    digest,
                    NeedleRecord::live(NeedleLocator {
                        volume_id,
                        payload_offset: offset,
                        size,
                    }),
                );
            }
            Self::Delete { digest } => {
                if let Some(rec) = map.get_mut(&digest) {
                    rec.tombstone();
                }
            }
            Self::Quarantine { digest } => {
                if let Some(rec) = map.get_mut(&digest) {
                    rec.quarantine();
                }
            }
        }
    }
}

/// Append-only, fsynced handle on `volumes/needles.log`.
///
/// Tracks logical length so appends always seek to EOF even if another process
/// truncated; [`Self::truncate`] clears after a successful checkpoint.
#[derive(Debug)]
struct IndexWal {
    file: std::fs::File,
    len: u64,
}

impl IndexWal {
    /// Open or create the WAL under `volumes_dir` (no truncate).
    fn open(volumes_dir: &Path) -> std::io::Result<Self> {
        let path = volumes_dir.join(LOG_FILE);
        let file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&path)?;
        let len = file.metadata()?.len();
        Ok(Self { file, len })
    }

    /// Append one NDJSON line and `sync_all` (crash-safe durability).
    fn append_line(&mut self, line: &[u8]) -> std::io::Result<()> {
        self.file.seek(SyncSeekFrom::Start(self.len))?;
        self.file.write_all(line)?;
        self.file.sync_all()?;
        self.len += line.len() as u64;
        Ok(())
    }

    /// Empty the WAL after `needles.json` covers every prior op.
    fn truncate(&mut self) -> std::io::Result<()> {
        self.file.set_len(0)?;
        self.file.sync_all()?;
        self.len = 0;
        Ok(())
    }
}

/// Opaque volume file identity (`volumes/<uuid>.dat`).
///
/// UUIDs make allocation trivial (`Uuid::new_v4`) — no `read_dir` max+1 scan and
/// no coordination between compaction and PUT beyond sealing the active FD.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct VolumeId(Uuid);

impl VolumeId {
    /// Allocate a fresh volume identity (`Uuid::new_v4`).
    fn new() -> Self {
        Self(Uuid::new_v4())
    }

    /// Parse a volume filename stem (`<uuid>.dat` → id).
    fn parse(name: &std::ffi::OsStr) -> Option<Self> {
        let name = name.to_str()?;
        let stem = name.strip_suffix(".dat")?;
        Uuid::parse_str(stem).ok().map(Self)
    }
}

impl std::fmt::Display for VolumeId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}

/// Where a live needle's bytes sit inside a volume file.
///
/// The in-memory / index map is `digest → NeedleLocator`. Offsets point at the
/// **payload**, not the needle header — GET seeks here and reads exactly
/// [`Self::size`] bytes (see module docs).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NeedleLocator {
    /// Volume that owns this needle (`volumes/<uuid>.dat`).
    pub volume_id: VolumeId,
    /// Byte offset of the payload start (after digest + size header).
    pub payload_offset: u64,
    /// Payload length in bytes (excludes the needle header).
    pub size: u64,
}

impl NeedleLocator {
    /// Construct a locator for a needle already written (or about to be remapped).
    fn new(volume_id: VolumeId, payload_offset: u64, size: u64) -> Self {
        Self {
            volume_id,
            payload_offset,
            size,
        }
    }
}

/// One index row: locator plus tombstone / quarantine flags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct NeedleRecord {
    locator: NeedleLocator,
    deleted: bool,
    quarantined: bool,
}

impl NeedleRecord {
    /// Live (servable) row immediately after a successful commit.
    fn live(locator: NeedleLocator) -> Self {
        Self {
            locator,
            deleted: false,
            quarantined: false,
        }
    }

    /// Whether GET may serve this needle (not deleted, not quarantined).
    fn is_live(self) -> bool {
        !self.deleted && !self.quarantined
    }

    /// Logical delete; physical reclaim is [`Haystack::compaction`].
    fn tombstone(&mut self) {
        self.deleted = true;
    }

    /// Integrity failure; GET refuses until compaction drops the row.
    fn quarantine(&mut self) {
        self.quarantined = true;
    }
}

/// Current append target: open FD + next write offset inside one volume.
#[derive(Debug)]
struct ActiveFile {
    volume_id: VolumeId,
    file: tokio::fs::File,
    /// Next needle's start offset (= EOF of the last complete needle).
    current_offset: u64,
}

impl ActiveFile {
    fn new(volume_id: VolumeId, file: tokio::fs::File, current_offset: u64) -> Self {
        Self {
            volume_id,
            file,
            current_offset,
        }
    }
}

/// Append-only volume packing for small CAS objects (Haystack / SeaweedFS lineage).
#[derive(Debug)]
pub struct Haystack {
    /// Directory holding `*.dat` volumes plus `needles.json` / `needles.log`.
    volumes_dir: PathBuf,
    /// Soft cap on one `*.dat` file; captured from env at [`Self::open`].
    max_volume_size: u64,
    /// Live digest → needle map (source of truth between checkpoints).
    index: parking_lot::Mutex<HashMap<Digest, NeedleRecord>>,
    /// Durable mutation log; truncated by [`Self::checkpoint`].
    wal: parking_lot::Mutex<IndexWal>,
    /// Set when the WAL has records not yet covered by `needles.json`.
    dirty: AtomicBool,
    /// Sole volume open for append; `None` when sealed / idle.
    active_file: tokio::sync::Mutex<Option<ActiveFile>>,
}

impl Haystack {
    /// Create `volumes/` under `root` and load the durable needle index.
    ///
    /// Volume soft-cap is `HAYSTACK_MAX_VOLUME_SIZE` (raw bytes), or
    /// [`DEFAULT_MAX_VOLUME_SIZE`] if unset/invalid — that env key is the only
    /// knob; it is read once here and stored on the instance.
    ///
    /// Boot: load `needles.json` (if present) then replay `needles.log`. If both
    /// are absent/empty but `*.dat` volumes exist, scan volumes (recovery) and
    /// checkpoint.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the directory cannot be created, the WAL cannot
    /// be opened, or recovery / checkpoint fails.
    pub fn open(root: impl AsRef<Path>) -> std::io::Result<Self> {
        let max_volume_size = std::env::var("HAYSTACK_MAX_VOLUME_SIZE")
            .ok()
            .and_then(|raw| raw.trim().parse().ok())
            .filter(|&n| n > 0)
            .unwrap_or(DEFAULT_MAX_VOLUME_SIZE)
            .max(NEEDLE_HEADER_LEN + 1);
        let volumes_dir = root.as_ref().join("volumes");
        std::fs::create_dir_all(&volumes_dir)?;
        let wal = IndexWal::open(&volumes_dir)?;

        let haystack = Self {
            volumes_dir,
            max_volume_size,
            index: parking_lot::Mutex::new(HashMap::new()),
            wal: parking_lot::Mutex::new(wal),
            dirty: AtomicBool::new(false),
            active_file: tokio::sync::Mutex::new(None),
        };
        haystack.bootstrap_index()?;
        Ok(haystack)
    }

    /// Soft cap for one volume file (bytes), including needle headers.
    pub fn max_volume_size(&self) -> u64 {
        self.max_volume_size
    }

    /// Path of one volume file (`volumes/<uuid>.dat`).
    fn volume_path(&self, volume_id: VolumeId) -> PathBuf {
        self.volumes_dir.join(format!("{volume_id}.{VOLUME_EXT}"))
    }

    #[inline]
    fn index_path(&self) -> PathBuf {
        self.volumes_dir.join(INDEX_FILE)
    }

    #[inline]
    fn log_path(&self) -> PathBuf {
        self.volumes_dir.join(LOG_FILE)
    }

    /// Load snapshot + replay WAL (or volume-scan recover) into the RAM index.
    fn bootstrap_index(&self) -> std::io::Result<()> {
        let mut map = match self.load_snapshot() {
            Ok(map) => map,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => HashMap::new(),
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "haystack needles.json unreadable; starting from empty snapshot + log replay"
                );
                HashMap::new()
            }
        };
        self.replay_log_into(&mut map)?;

        let has_volumes = self.volume_dat_exists()?;
        if map.is_empty() && has_volumes {
            tracing::warn!("haystack index empty but volumes present; recovering via volume scan");
            self.rebuild_index()?;
            self.checkpoint().map_err(|e| match e {
                AppError::Io(io) => io,
                other => std::io::Error::other(other),
            })?;
            return Ok(());
        }

        *self.index.lock() = map;
        // Ensure a snapshot exists for tooling even on an empty fresh store.
        if !self.index_path().is_file() {
            self.checkpoint().map_err(|e| match e {
                AppError::Io(io) => io,
                other => std::io::Error::other(other),
            })?;
        }
        Ok(())
    }

    /// Whether any `*.dat` volume file exists under [`Self::volumes_dir`].
    fn volume_dat_exists(&self) -> std::io::Result<bool> {
        for entry in std::fs::read_dir(&self.volumes_dir)? {
            let entry = entry?;
            if VolumeId::parse(entry.file_name().as_os_str()).is_some() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Deserialize `needles.json` into a RAM map (does not touch the WAL).
    ///
    /// # Errors
    ///
    /// Returns [`NotFound`](std::io::ErrorKind::NotFound) if the snapshot is
    /// missing, or [`InvalidData`](std::io::ErrorKind::InvalidData) for bad
    /// JSON / unsupported version / bad digests.
    fn load_snapshot(&self) -> std::io::Result<HashMap<Digest, NeedleRecord>> {
        let file = std::fs::File::open(self.index_path())?;
        let parsed: NeedleIndexFile = serde_json::from_reader(file).map_err(|e| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("needles.json: {e}"),
            )
        })?;
        if parsed.version != INDEX_VERSION {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "needles.json: unsupported version {} (want {INDEX_VERSION})",
                    parsed.version
                ),
            ));
        }

        let mut map = HashMap::with_capacity(parsed.entries.len());
        for entry in parsed.entries {
            let digest = Digest::parse(entry.digest.as_str()).map_err(|e| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("needles.json entry digest: {e}"),
                )
            })?;
            map.insert(
                digest,
                NeedleRecord {
                    locator: NeedleLocator {
                        volume_id: entry.volume_id,
                        payload_offset: entry.offset,
                        size: entry.size,
                    },
                    deleted: entry.deleted,
                    quarantined: entry.quarantined,
                },
            );
        }
        Ok(map)
    }

    /// Apply every complete NDJSON line from `needles.log` onto `map`.
    ///
    /// Stops (with a warning) at the first unreadable line so a torn trailing
    /// append after a crash does not discard earlier ops.
    fn replay_log_into(&self, map: &mut HashMap<Digest, NeedleRecord>) -> std::io::Result<()> {
        let path = self.log_path();
        if !path.is_file() {
            return Ok(());
        }
        let file = std::fs::File::open(&path)?;
        let reader = BufReader::new(file);
        for (line_no, line) in reader.lines().enumerate() {
            let line = line?;
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            match serde_json::from_str::<IndexLogOp>(line) {
                Ok(op) => op.apply_to(map),
                Err(err) => {
                    // Torn last line after a crash — stop; earlier ops already applied.
                    tracing::warn!(
                        line = line_no + 1,
                        error = %err,
                        "haystack needles.log: stopping replay at unreadable line"
                    );
                    break;
                }
            }
        }
        Ok(())
    }

    /// Serialize `op`, append + fsync to the WAL, and mark the index dirty.
    fn append_wal(&self, op: IndexLogOp) -> Result<(), AppError> {
        let mut line = serde_json::to_vec(&op)?;
        line.push(b'\n');
        let mut wal = self.wal.lock();
        wal.append_line(&line)?;
        self.dirty.store(true, Ordering::Release);
        Ok(())
    }

    /// Rewrite `needles.json` from RAM and truncate `needles.log`.
    ///
    /// Holds the WAL lock so no append can sneak in between snapshot and truncate.
    /// Safe to call from the background task or tests.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] if JSON encoding, the atomic snapshot write, or
    /// WAL truncate fails.
    pub fn checkpoint(&self) -> Result<(), AppError> {
        let mut wal = self.wal.lock();
        let file = {
            let guard = self.index.lock();
            let entries: Vec<NeedleIndexEntry> = guard
                .iter()
                .map(|(digest, rec)| NeedleIndexEntry::from_record(digest, rec))
                .collect();
            NeedleIndexFile {
                version: INDEX_VERSION,
                entries,
            }
        };
        let buf = serde_json::to_vec_pretty(&file)?;
        atomic_write_sibling_sync(&self.index_path(), &buf)?;
        wal.truncate()?;
        self.dirty.store(false, Ordering::Release);
        Ok(())
    }

    /// Whether the WAL has ops not yet covered by a checkpointed snapshot.
    pub fn is_index_dirty(&self) -> bool {
        self.dirty.load(Ordering::Acquire)
    }

    /// Framed needle length: header + payload.
    #[inline]
    const fn needle_len(payload_len: u64) -> u64 {
        NEEDLE_HEADER_LEN + payload_len
    }

    /// Whether a payload of `payload_len` bytes fits in one volume (header included).
    pub fn fits_in_volume(&self, payload_len: u64) -> bool {
        Self::needle_len(payload_len) <= self.max_volume_size
    }

    /// Digests that are currently servable (not deleted, not quarantined).
    pub fn indexed_digests(&self) -> Vec<Digest> {
        self.index
            .lock()
            .iter()
            .filter(|(_, rec)| rec.is_live())
            .map(|(d, _)| d.clone())
            .collect()
    }

    /// Ensure `active_file` is an open, writable volume with room for `size_to_append`.
    ///
    /// Closed volumes are **immutable**: once the active FD is dropped (full
    /// volume, or sealed for compaction), that `*.dat` is never reopened for
    /// append. A new write always `create_new`s the next volume id.
    async fn ensure_active_file(
        &self,
        active: &mut Option<ActiveFile>,
        size_to_append: u64,
    ) -> Result<(), AppError> {
        if let Some(file) = active.as_ref() {
            if file.current_offset + size_to_append <= self.max_volume_size {
                return Ok(());
            }
        }

        let volume_id = VolumeId::new();
        let path = self.volume_path(volume_id);
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .await?;
        *active = Some(ActiveFile::new(volume_id, file, 0));
        Ok(())
    }

    /// Directory that holds append-only volume files (`…/volumes`).
    pub fn volumes_dir(&self) -> &Path {
        &self.volumes_dir
    }

    /// Occupancy for metrics: `(volume_file_count, total_bytes_on_disk)`.
    ///
    /// Counts only `*.dat` volume files — ignores `needles.json` and junk sidecars.
    ///
    /// # Errors
    ///
    /// Propagates I/O errors from directory reads or metadata lookups.
    pub fn scan_occupancy(&self) -> std::io::Result<(u64, u64)> {
        let mut file_count = 0u64;
        let mut total_bytes = 0u64;
        for entry in std::fs::read_dir(&self.volumes_dir)? {
            let entry = entry?;
            if VolumeId::parse(entry.file_name().as_os_str()).is_none() {
                continue;
            }
            let metadata = entry.metadata()?;
            if metadata.is_file() {
                file_count += 1;
                total_bytes += metadata.len();
            }
        }
        Ok((file_count, total_bytes))
    }

    /// Compact dirty volumes by copying live needles into a **new** volume id.
    ///
    /// Closed volumes are immutable: this never rewrites an existing `*.dat` in
    /// place. Per dirty volume:
    /// 1. Seal it if it is the active append target (drop the FD).
    /// 2. Snapshot live needles; stage them into a sibling temp under
    ///    `volumes/`, `sync_all`, then [`publish_temp`] to
    ///    `volumes/<new-uuid>.dat` so a crash never leaves a half-written
    ///    final path.
    /// 3. Under the index lock, remap `old@off → new@off` and drop dead rows.
    /// 4. Checkpoint, then unlink the old file.
    ///
    /// Peak disk ≈ old + live (+ temp during the copy). Empty (all-dead)
    /// volumes skip the new file and are unlinked after the index drop.
    ///
    /// Returns digests whose index rows were dropped (tombstone / quarantine /
    /// empty-volume reclaim). Callers that keep a separate routing map should
    /// remove only these keys — never rebuild the whole map, or a concurrent
    /// commit's insert can be clobbered.
    ///
    /// # Errors
    ///
    /// Returns an I/O-backed [`AppError`] if a volume cannot be read/written or
    /// the checkpoint fails.
    pub async fn compaction(&self) -> Result<Vec<Digest>, AppError> {
        let dirty_volumes: HashSet<VolumeId> = {
            let guard = self.index.lock();
            guard
                .values()
                .filter(|rec| !rec.is_live())
                .map(|rec| rec.locator.volume_id)
                .collect()
        };

        let mut dropped = Vec::new();

        for old_id in dirty_volumes {
            // Seal before snapshot so no PUT can append into the volume we are
            // about to retire. Holding `active_file` only for the seal keeps
            // the long copy off the append mutex.
            {
                let mut active = self.active_file.lock().await;
                if active.as_ref().is_some_and(|a| a.volume_id == old_id) {
                    *active = None;
                }
            }

            let live: Vec<(Digest, NeedleLocator)> = {
                self.index
                    .lock()
                    .iter()
                    .filter(|(_, rec)| rec.locator.volume_id == old_id && rec.is_live())
                    .map(|(digest, rec)| (digest.clone(), rec.locator))
                    .collect()
            };
            let old_path = self.volume_path(old_id);

            if live.is_empty() {
                {
                    let mut guard = self.index.lock();
                    dropped.extend(
                        guard
                            .iter()
                            .filter(|(_, rec)| rec.locator.volume_id == old_id)
                            .map(|(digest, _)| digest.clone()),
                    );
                    guard.retain(|_, rec| rec.locator.volume_id != old_id);
                }
                self.checkpoint()?;
                let _ = tokio::fs::remove_file(&old_path).await;
                continue;
            }

            let new_id = VolumeId::new();
            let dest = self.volume_path(new_id);
            // Same-dir temp so rename is atomic; TempEntry unlinks on failure.
            let mut temp = TempEntry::unique_in(&self.volumes_dir, "compact");
            let mut out = tokio::fs::File::create(temp.path()).await?;
            let mut src = tokio::fs::File::open(&old_path).await?;
            let mut buffer = vec![0u8; 1024 * 1024];
            let mut needle_start = 0u64;
            let mut new_locs: Vec<(Digest, NeedleLocator)> = Vec::with_capacity(live.len());

            for (digest, locator) in &live {
                out.write_all(digest.as_str().as_bytes()).await?;
                out.write_all(&locator.size.to_le_bytes()).await?;

                src.seek(std::io::SeekFrom::Start(locator.payload_offset))
                    .await?;
                let mut remaining = locator.size;
                while remaining > 0 {
                    let n = remaining.min(buffer.len() as u64) as usize;
                    src.read_exact(&mut buffer[..n]).await?;
                    out.write_all(&buffer[..n]).await?;
                    remaining -= n as u64;
                }

                let payload_offset = needle_start + NEEDLE_HEADER_LEN;
                let locator = NeedleLocator::new(new_id, payload_offset, locator.size);
                new_locs.push((digest.clone(), locator));
                needle_start += Self::needle_len(locator.size);
            }
            out.sync_all().await?;
            drop(out);
            drop(src);

            publish_temp(temp.path(), &dest).await?;
            temp.disarm();

            {
                let mut guard = self.index.lock();
                new_locs.into_iter().for_each(|(digest, loc)| {
                    let Some(rec) = guard.get_mut(&digest) else {
                        return;
                    };
                    // Tombstoned/quarantined during the copy: leave on old_id
                    // so the retain below drops the row (orphan bytes in the
                    // new volume until a later compact).
                    if rec.is_live() {
                        rec.locator = loc;
                    }
                });
                dropped.extend(
                    guard
                        .iter()
                        .filter(|(_, rec)| rec.locator.volume_id == old_id)
                        .map(|(digest, _)| digest.clone()),
                );
                guard.retain(|_, rec| rec.locator.volume_id != old_id);
            }
            self.checkpoint()?;
            let _ = tokio::fs::remove_file(&old_path).await;
        }

        Ok(dropped)
    }

    /// Whether `digest` is currently servable (present, not deleted/quarantined).
    pub fn contains(&self, digest: &Digest) -> bool {
        self.index
            .lock()
            .get(digest)
            .is_some_and(|rec| rec.is_live())
    }

    /// Return the live [`NeedleLocator`] for `digest`, if servable.
    pub fn locate(&self, digest: &Digest) -> Option<NeedleLocator> {
        self.index
            .lock()
            .get(digest)
            .filter(|rec| rec.is_live())
            .map(|rec| rec.locator)
    }

    /// Append `temp`'s bytes as a framed needle and index `digest`.
    ///
    /// Durability: header + payload are `sync_all`'d into the volume, then one
    /// WAL record is appended to `needles.log` and the RAM map is updated. A
    /// background checkpoint later rewrites `needles.json`. The staging `temp`
    /// is removed on success (callers disarm their
    /// [`crate::durable::TempEntry`] afterward).
    ///
    /// # Errors
    ///
    /// Returns an I/O-backed [`AppError`] if the volume cannot be opened,
    /// appended, synced, the WAL cannot be written, or if `temp` cannot be
    /// removed.
    ///
    /// # Panics
    ///
    /// Panics if the active-volume ensure step returns without installing an
    /// active volume (programming error).
    pub async fn commit_temp(&self, temp: &Path, digest: &Digest) -> Result<(), AppError> {
        let temp_size = tokio::fs::metadata(temp).await?.len();
        let needle_len = Self::needle_len(temp_size);

        let mut guard = self.active_file.lock().await;
        self.ensure_active_file(&mut guard, needle_len).await?;
        let active = guard
            .as_mut()
            .expect("ensure_active_file left an active volume");

        let needle_start = active.current_offset;
        let payload_offset = needle_start + NEEDLE_HEADER_LEN;
        let locator = NeedleLocator {
            volume_id: active.volume_id,
            payload_offset,
            size: temp_size,
        };

        active
            .file
            .seek(std::io::SeekFrom::Start(needle_start))
            .await?;
        let mut src = tokio::fs::File::open(temp).await?;
        active.file.write_all(digest.as_str().as_bytes()).await?;
        active.file.write_all(&temp_size.to_le_bytes()).await?;
        tokio::io::copy(&mut src, &mut active.file).await?;
        active.file.sync_all().await?;
        active.current_offset = needle_start + needle_len;
        drop(guard);

        // Volume durable → WAL → RAM (reboot replays WAL if we crash after this).
        self.append_wal(IndexLogOp::put(digest, locator))?;
        self.index
            .lock()
            .insert(digest.clone(), NeedleRecord::live(locator));

        tokio::fs::remove_file(temp).await?;
        Ok(())
    }

    /// Open needle **payload** bytes for `digest`, capped at the locator size.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] if missing or tombstoned, an integrity
    /// error if quarantined, or an I/O-backed [`AppError`] if the volume cannot
    /// be opened / seeked.
    pub async fn open_blob(&self, digest: &Digest) -> Result<Take<tokio::fs::File>, AppError> {
        self.ensure_readable(digest)?;
        let locator = self.locate(digest).ok_or(AppError::NoSuchKey)?;
        let mut file = tokio::fs::File::open(self.volume_path(locator.volume_id)).await?;
        file.seek(std::io::SeekFrom::Start(locator.payload_offset))
            .await?;
        Ok(file.take(locator.size))
    }

    /// Open an inclusive byte range within a needle's payload.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] if missing/tombstoned, an integrity error
    /// if quarantined, [`AppError::InvalidRequest`] if the range is outside the
    /// payload, or an I/O-backed [`AppError`].
    pub async fn open_blob_range(
        &self,
        digest: &Digest,
        start: u64,
        end: u64,
    ) -> Result<Take<tokio::fs::File>, AppError> {
        self.ensure_readable(digest)?;
        let locator = self.locate(digest).ok_or(AppError::NoSuchKey)?;
        if start > end || end >= locator.size {
            return Err(AppError::InvalidRequest(format!(
                "invalid range: start={start} end={end} needle_size={}",
                locator.size
            )));
        }
        let mut file = tokio::fs::File::open(self.volume_path(locator.volume_id)).await?;
        file.seek(std::io::SeekFrom::Start(locator.payload_offset + start))
            .await?;
        Ok(file.take(end - start + 1))
    }

    /// Refuse reads of quarantined digests; missing/tombstoned are handled by callers.
    fn ensure_readable(&self, digest: &Digest) -> Result<(), AppError> {
        let guard = self.index.lock();
        match guard.get(digest) {
            Some(rec) if rec.quarantined => Err(AppError::Other(anyhow::anyhow!(
                "blob failed integrity check"
            ))),
            _ => Ok(()),
        }
    }

    /// Tombstone a digest (`deleted: true`); physical reclaim is compaction.
    ///
    /// Idempotent: missing or already-deleted digests return `Ok(None)`. The
    /// locator stays until compaction; durability is a WAL `delete` record.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] if the WAL append fails.
    pub async fn remove(&self, digest: &Digest) -> Result<Option<u64>, AppError> {
        let size = {
            let guard = self.index.lock();
            match guard.get(digest) {
                Some(rec) if !rec.deleted => Some(rec.locator.size),
                _ => None,
            }
        };
        let Some(size) = size else {
            return Ok(None);
        };
        self.append_wal(IndexLogOp::Delete {
            digest: digest.clone(),
        })?;
        if let Some(rec) = self.index.lock().get_mut(digest) {
            rec.tombstone();
        }
        Ok(Some(size))
    }

    /// Mark a digest quarantined after a scrub mismatch; GET will refuse it.
    ///
    /// No-op (returns `false`) if the digest is absent. Appends a WAL record
    /// when the flag flips.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] if the WAL append fails.
    pub fn mark_quarantined(&self, digest: &Digest) -> Result<bool, AppError> {
        let should = {
            let guard = self.index.lock();
            matches!(guard.get(digest), Some(rec) if !rec.quarantined)
        };
        if !should {
            return Ok(false);
        }
        self.append_wal(IndexLogOp::Quarantine {
            digest: digest.clone(),
        })?;
        if let Some(rec) = self.index.lock().get_mut(digest) {
            rec.quarantine();
        }
        Ok(true)
    }

    /// Re-hash every live needle; quarantine digests whose payload ≠ name.
    ///
    /// Walks the durable index (not a volume scan — recovery can truncate).
    /// For each live locator: seek to payload offset, hash exactly `size`
    /// bytes, compare to the digest key. Mismatch or short read →
    /// [`Self::mark_quarantined`]. Returns how many needles were examined.
    ///
    /// # Errors
    ///
    /// Returns an I/O-backed [`AppError`] if a volume cannot be opened/read, or
    /// if quarantine WAL append fails.
    pub async fn scrub_once(&self) -> Result<u64, AppError> {
        let mut by_volume: HashMap<VolumeId, Vec<(Digest, NeedleLocator)>> = HashMap::new();
        {
            let guard = self.index.lock();
            for (digest, rec) in guard.iter() {
                if !rec.is_live() {
                    continue;
                }
                by_volume
                    .entry(rec.locator.volume_id)
                    .or_default()
                    .push((digest.clone(), rec.locator));
            }
        }

        let mut buffer = vec![0u8; 1024 * 1024];
        let mut examined = 0u64;

        for (volume_id, needles) in by_volume {
            let mut file = tokio::fs::File::open(self.volume_path(volume_id)).await?;
            for (digest, locator) in needles {
                examined += 1;
                file.seek(std::io::SeekFrom::Start(locator.payload_offset))
                    .await?;

                let mut hasher = Sha256::new();
                let mut remaining = locator.size;
                let mut short_read = false;
                while remaining > 0 {
                    let want = remaining.min(buffer.len() as u64) as usize;
                    let n = file.read(&mut buffer[..want]).await?;
                    if n == 0 {
                        short_read = true;
                        break;
                    }
                    hasher.update(&buffer[..n]);
                    remaining -= n as u64;
                }

                let calculated = Digest::from_bytes(&hasher.finalize())?;
                if short_read || calculated != digest {
                    self.mark_quarantined(&digest)?;
                }
            }
        }

        Ok(examined)
    }

    /// Scan every `*.dat` volume and rebuild the in-memory needle map.
    ///
    /// **Recovery path** when snapshot+log are empty but volumes exist. Each
    /// volume is scanned on its own thread ([`std::thread::scope`]). Incomplete
    /// trailing needles are not indexed; that volume is truncated to the last
    /// complete needle. Callers should [`Self::checkpoint`] afterward (clears
    /// the WAL and writes a fresh snapshot).
    ///
    /// # Errors
    ///
    /// Returns an I/O error if directory listing or a per-volume scan fails, or
    /// if a scan thread panics.
    fn rebuild_index(&self) -> std::io::Result<()> {
        let volumes = self.list_volume_files()?;
        let mut needles = HashMap::new();

        std::thread::scope(|scope| -> std::io::Result<()> {
            let mut handles = Vec::with_capacity(volumes.len());
            for (volume_id, path) in &volumes {
                handles.push(scope.spawn(|| Self::scan_volume(*volume_id, path)));
            }
            for handle in handles {
                let partial = handle
                    .join()
                    .map_err(|_| std::io::Error::other("volume scan thread panicked"))?;
                needles.extend(partial?);
            }
            Ok(())
        })?;

        *self.index.lock() = needles;
        Ok(())
    }

    /// List `(volume_id, path)` for every parseable `*.dat` under `volumes/`.
    fn list_volume_files(&self) -> std::io::Result<Vec<(VolumeId, PathBuf)>> {
        let mut volumes = Vec::new();
        for entry in std::fs::read_dir(&self.volumes_dir)? {
            let entry = entry?;
            if !entry.metadata()?.is_file() {
                continue;
            }
            let Some(volume_id) = VolumeId::parse(entry.file_name().as_os_str()) else {
                continue;
            };
            volumes.push((volume_id, entry.path()));
        }
        Ok(volumes)
    }

    /// Walk one volume file and index every complete needle found in it.
    ///
    /// Reads sequential `[digest hex][size LE][payload]` frames (see module
    /// docs). Each complete needle becomes a live [`NeedleRecord`] whose
    /// [`NeedleLocator::payload_offset`] points at the **payload** start. Stops at the
    /// first incomplete / corrupt frame (short header, non-UTF8 / non-hex
    /// digest, or payload past EOF) and **truncates** the file to
    /// `good_end` so a torn trailing append cannot be served or extended over.
    ///
    /// Used only by [`Self::rebuild_index`] (missing/corrupt `needles.json`).
    /// Do not call during scrub — truncation is a recovery side effect, and
    /// tombstone / quarantine flags live only in the durable index.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the volume cannot be opened, read, seeked, or
    /// truncated. Parse failures end the walk; they are not hard errors.
    fn scan_volume(
        volume_id: VolumeId,
        path: &Path,
    ) -> std::io::Result<HashMap<Digest, NeedleRecord>> {
        let mut needles = HashMap::new();
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)?;
        let file_len = file.metadata()?.len();
        let mut good_end = 0u64;
        let mut header = [0u8; NEEDLE_HEADER_LEN as usize];

        loop {
            let header_pos = file.stream_position()?;
            if header_pos + NEEDLE_HEADER_LEN > file_len {
                break;
            }

            match file.read_exact(&mut header) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::UnexpectedEof => break,
                Err(err) => return Err(err),
            }

            let (digest_bytes, size_bytes) = (&header[..Digest::LEN], &header[Digest::LEN..]);
            let Ok(digest_str) = std::str::from_utf8(digest_bytes) else {
                break;
            };
            let Ok(digest) = Digest::parse(digest_str) else {
                break;
            };

            let size = u64::from_le_bytes(size_bytes.try_into().expect("u64 header slice"));
            let payload_pos = header_pos + NEEDLE_HEADER_LEN;
            if payload_pos.saturating_add(size) > file_len {
                // Torn payload — drop this needle and truncate.
                break;
            }

            needles.insert(
                digest,
                NeedleRecord::live(NeedleLocator {
                    volume_id,
                    payload_offset: payload_pos,
                    size,
                }),
            );
            good_end = payload_pos + size;
            file.seek(SyncSeekFrom::Start(good_end))?;
        }

        if good_end < file_len {
            file.set_len(good_end)?;
        }
        Ok(needles)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

    fn digest_of(bytes: &[u8]) -> Digest {
        Digest(hex::encode(Sha256::digest(bytes)))
    }

    /// Deterministic volume id for recovery / parse tests (`n` → fixed UUID).
    fn vid(n: u128) -> VolumeId {
        VolumeId(Uuid::from_u128(n))
    }

    fn volume_dat(dir: &Path, id: VolumeId) -> PathBuf {
        dir.join(format!("{id}.dat"))
    }

    async fn stage(dir: &Path, name: &str, bytes: &[u8]) -> PathBuf {
        let path = dir.join(name);
        tokio::fs::write(&path, bytes).await.unwrap();
        path
    }

    fn expected_occupancy(payload_lens: &[usize]) -> u64 {
        payload_lens
            .iter()
            .map(|&n| NEEDLE_HEADER_LEN + n as u64)
            .sum()
    }

    fn write_needle(path: &Path, digest: &Digest, payload: &[u8]) {
        use std::io::Write;
        let mut f = std::fs::File::create(path).unwrap();
        f.write_all(digest.as_str().as_bytes()).unwrap();
        f.write_all(&(payload.len() as u64).to_le_bytes()).unwrap();
        f.write_all(payload).unwrap();
    }

    async fn read_all(hs: &Haystack, digest: &Digest) -> Vec<u8> {
        let mut reader = hs.open_blob(digest).await.unwrap();
        let mut got = Vec::new();
        reader.read_to_end(&mut got).await.unwrap();
        got
    }

    #[test]
    fn open_creates_volumes_dir_and_empty_index() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.volumes_dir().is_dir());
        assert!(hs.volumes_dir().ends_with("volumes"));
        assert!(
            hs.index_path().is_file(),
            "empty needles.json written on open"
        );
        assert!(!hs.contains(&Digest("00".repeat(32))));
        assert_eq!(hs.locate(&Digest("00".repeat(32))), None);
        assert_eq!(hs.scan_occupancy().unwrap(), (0, 0));
    }

    #[tokio::test]
    async fn commit_temp_appends_needle_and_indexes_digest() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"needle-bytes";
        let digest = digest_of(bytes);
        let temp = stage(root.path(), "upload.tmp", bytes).await;

        hs.commit_temp(&temp, &digest).await.expect("commit");

        assert!(!temp.exists(), "staging temp must be consumed");
        assert!(hs.contains(&digest));
        let locator = hs.locate(&digest).expect("indexed");
        assert_eq!(locator.payload_offset, NEEDLE_HEADER_LEN);
        assert_eq!(locator.size, bytes.len() as u64);
        assert!(hs.volume_path(locator.volume_id).exists());
        assert_eq!(
            hs.scan_occupancy().unwrap(),
            (1, expected_occupancy(&[bytes.len()]))
        );

        let mut reader = hs.open_blob(&digest).await.unwrap();
        let mut got = Vec::new();
        reader.read_to_end(&mut got).await.unwrap();
        assert_eq!(got, bytes);
    }

    #[tokio::test]
    async fn commit_temp_packs_two_needles_in_one_volume() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let a = b"aaa";
        let b = b"bbbb";
        let da = digest_of(a);
        let db = digest_of(b);

        hs.commit_temp(&stage(root.path(), "a.tmp", a).await, &da)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "b.tmp", b).await, &db)
            .await
            .unwrap();

        let a_payload = NEEDLE_HEADER_LEN;
        let b_payload = Haystack::needle_len(a.len() as u64) + NEEDLE_HEADER_LEN;
        let loc_a = hs.locate(&da).unwrap();
        let loc_b = hs.locate(&db).unwrap();
        assert_eq!(
            loc_a.volume_id, loc_b.volume_id,
            "both needles share a volume"
        );
        assert_eq!(loc_a.payload_offset, a_payload);
        assert_eq!(loc_a.size, 3);
        assert_eq!(loc_b.payload_offset, b_payload);
        assert_eq!(loc_b.size, 4);
        assert_eq!(
            hs.scan_occupancy().unwrap(),
            (1, expected_occupancy(&[3, 4]))
        );

        let mut ra = hs.open_blob(&da).await.unwrap();
        let mut ga = Vec::new();
        ra.read_to_end(&mut ga).await.unwrap();
        assert_eq!(ga, a);

        let mut rb = hs.open_blob(&db).await.unwrap();
        let mut gb = Vec::new();
        rb.read_to_end(&mut gb).await.unwrap();
        assert_eq!(gb, b);
    }

    #[tokio::test]
    async fn open_rebuilds_index_from_volumes() {
        let root = TempDir::new().unwrap();
        let bytes = b"survive-restart";
        let digest = digest_of(bytes);

        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
        }

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        let locator = hs.locate(&digest).unwrap();
        assert_eq!(locator.payload_offset, NEEDLE_HEADER_LEN);
        assert_eq!(locator.size, bytes.len() as u64);
        let mut reader = hs.open_blob(&digest).await.unwrap();
        let mut got = Vec::new();
        reader.read_to_end(&mut got).await.unwrap();
        assert_eq!(got, bytes);
    }

    #[tokio::test]
    async fn rebuild_truncates_torn_tail() {
        let root = TempDir::new().unwrap();
        let bytes = b"complete";
        let digest = digest_of(bytes);

        let volume = {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            hs.volume_path(hs.locate(&digest).unwrap().volume_id)
        };

        let good_len = expected_occupancy(&[bytes.len()]);
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&volume)
                .unwrap();
            // Partial header only — not a complete needle.
            f.write_all(b"deadbeef").unwrap();
        }
        assert!(std::fs::metadata(&volume).unwrap().len() > good_len);

        // Torn-tail truncate runs on the recovery path (no snapshot + no WAL).
        std::fs::remove_file(root.path().join("volumes").join(INDEX_FILE)).unwrap();
        let _ = std::fs::remove_file(root.path().join("volumes").join(LOG_FILE));

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(std::fs::metadata(&volume).unwrap().len(), good_len);
        assert!(hs.index_path().is_file());
    }

    #[tokio::test]
    async fn open_blob_does_not_spill_into_next_needle() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let a = b"aaa";
        let b = b"bbbbbbbb";
        let da = digest_of(a);
        let db = digest_of(b);
        hs.commit_temp(&stage(root.path(), "a.tmp", a).await, &da)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "b.tmp", b).await, &db)
            .await
            .unwrap();

        let mut reader = hs.open_blob(&da).await.unwrap();
        let mut got = Vec::new();
        reader.read_to_end(&mut got).await.unwrap();
        assert_eq!(got, a);
    }

    #[tokio::test]
    async fn parallel_rebuild_indexes_needles_across_volumes() {
        let root = TempDir::new().unwrap();
        let volumes = root.path().join("volumes");
        std::fs::create_dir_all(&volumes).unwrap();

        let a = b"vol-zero";
        let b = b"vol-one";
        let da = digest_of(a);
        let db = digest_of(b);
        let vol_a = vid(1);
        let vol_b = vid(2);

        write_needle(&volume_dat(&volumes, vol_a), &da, a);
        write_needle(&volume_dat(&volumes, vol_b), &db, b);

        let hs = Haystack::open(root.path()).unwrap();
        assert_eq!(
            hs.locate(&da).unwrap(),
            NeedleLocator {
                volume_id: vol_a,
                payload_offset: NEEDLE_HEADER_LEN,
                size: a.len() as u64,
            }
        );
        assert_eq!(
            hs.locate(&db).unwrap(),
            NeedleLocator {
                volume_id: vol_b,
                payload_offset: NEEDLE_HEADER_LEN,
                size: b.len() as u64,
            }
        );

        assert_eq!(read_all(&hs, &da).await, a);
        assert_eq!(read_all(&hs, &db).await, b);
    }

    #[test]
    fn volume_id_parse_accepts_uuid_dat_stems_and_rejects_junk() {
        let id = vid(42);
        let name = format!("{id}.dat");
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new(&name)), Some(id));
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new("0.dat")), None);
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new("0.idx")), None);
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new("notes.txt")), None);
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new(".dat")), None);
        assert_eq!(
            VolumeId::parse(std::ffi::OsStr::new("not-a-uuid.dat")),
            None
        );
    }

    #[tokio::test]
    async fn open_blob_missing_digest_is_no_such_key() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let missing = digest_of(b"never-committed");
        assert!(matches!(
            hs.open_blob(&missing).await,
            Err(AppError::NoSuchKey)
        ));
        assert!(matches!(
            hs.open_blob_range(&missing, 0, 0).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn open_blob_range_reads_inclusive_slice() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"abcdefgh";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
            .await
            .unwrap();

        let mut mid = hs.open_blob_range(&digest, 2, 5).await.unwrap();
        let mut got = Vec::new();
        mid.read_to_end(&mut got).await.unwrap();
        assert_eq!(got, b"cdef");

        let mut one = hs.open_blob_range(&digest, 0, 0).await.unwrap();
        let mut first = Vec::new();
        one.read_to_end(&mut first).await.unwrap();
        assert_eq!(first, b"a");

        let mut last = hs.open_blob_range(&digest, 7, 7).await.unwrap();
        let mut tail = Vec::new();
        last.read_to_end(&mut tail).await.unwrap();
        assert_eq!(tail, b"h");
    }

    #[tokio::test]
    async fn open_blob_range_rejects_invalid_bounds() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"abcd";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
            .await
            .unwrap();

        assert!(matches!(
            hs.open_blob_range(&digest, 2, 1).await,
            Err(AppError::InvalidRequest(_))
        ));
        assert!(matches!(
            hs.open_blob_range(&digest, 0, 4).await,
            Err(AppError::InvalidRequest(_))
        ));
        assert!(matches!(
            hs.open_blob_range(&digest, 4, 4).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn remove_tombstones_but_leaves_volume_bytes_and_index_row() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"reclaim-later";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
            .await
            .unwrap();

        let size = hs.remove(&digest).await.unwrap();
        assert_eq!(size, Some(bytes.len() as u64));
        assert!(!hs.contains(&digest));
        assert!(matches!(
            hs.open_blob(&digest).await,
            Err(AppError::NoSuchKey)
        ));
        // Idempotent.
        assert_eq!(hs.remove(&digest).await.unwrap(), None);
        // Physical volume still holds the framed needle until compaction.
        assert_eq!(
            hs.scan_occupancy().unwrap(),
            (1, expected_occupancy(&[bytes.len()]))
        );
        // Tombstone retained after checkpoint for compaction tooling.
        hs.checkpoint().unwrap();
        let file: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert_eq!(file.entries.len(), 1);
        assert_eq!(file.entries[0].digest, digest);
        assert!(file.entries[0].deleted);
        assert!(!file.entries[0].quarantined);
    }

    #[tokio::test]
    async fn commit_empty_payload_round_trips() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "empty.tmp", bytes).await, &digest)
            .await
            .unwrap();

        assert_eq!(
            hs.locate(&digest).unwrap().payload_offset,
            NEEDLE_HEADER_LEN
        );
        assert_eq!(hs.locate(&digest).unwrap().size, 0);
        assert_eq!(read_all(&hs, &digest).await, b"");
        assert!(matches!(
            hs.open_blob_range(&digest, 0, 0).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn commit_rolls_to_next_volume_when_full() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let max = hs.max_volume_size();

        let big_len = (max - NEEDLE_HEADER_LEN) as usize;
        let big = vec![b'x'; big_len];
        let small = b"y";
        let d_big = digest_of(&big);
        let d_small = digest_of(small);

        hs.commit_temp(&stage(root.path(), "big.tmp", &big).await, &d_big)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "small.tmp", small).await, &d_small)
            .await
            .unwrap();

        assert_ne!(
            hs.locate(&d_big).unwrap().volume_id,
            hs.locate(&d_small).unwrap().volume_id,
            "full volume must roll to a new uuid volume"
        );
        assert_eq!(hs.scan_occupancy().unwrap().0, 2);
        assert_eq!(read_all(&hs, &d_big).await, big);
        assert_eq!(read_all(&hs, &d_small).await, small);
    }

    #[tokio::test]
    async fn rebuild_skips_non_volume_files_in_volumes_dir() {
        let root = TempDir::new().unwrap();
        let volumes = root.path().join("volumes");
        std::fs::create_dir_all(&volumes).unwrap();

        let bytes = b"keep-me";
        let digest = digest_of(bytes);
        write_needle(&volume_dat(&volumes, vid(7)), &digest, bytes);
        std::fs::write(volumes.join("notes.txt"), b"not a volume").unwrap();
        std::fs::write(volumes.join("0.idx"), b"sidecar").unwrap();
        std::fs::write(volumes.join("0.dat"), b"numeric stem is not a uuid volume").unwrap();

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
        // Occupancy counts only *.dat — junk + needles.json are ignored.
        assert_eq!(hs.scan_occupancy().unwrap().0, 1);
        assert!(hs.index_path().is_file());
    }

    #[tokio::test]
    async fn rebuild_truncates_torn_payload_after_valid_header() {
        let root = TempDir::new().unwrap();
        let volumes = root.path().join("volumes");
        std::fs::create_dir_all(&volumes).unwrap();

        let good = b"complete-needle";
        let digest = digest_of(good);
        let volume = volume_dat(&volumes, vid(3));
        write_needle(&volume, &digest, good);

        // Append a header that claims a large payload, then only a few bytes.
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&volume)
                .unwrap();
            let fake = digest_of(b"torn");
            f.write_all(fake.as_str().as_bytes()).unwrap();
            f.write_all(&1000u64.to_le_bytes()).unwrap();
            f.write_all(b"nope").unwrap();
        }
        let good_len = expected_occupancy(&[good.len()]);
        assert!(std::fs::metadata(&volume).unwrap().len() > good_len);

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert!(!hs.contains(&digest_of(b"torn")));
        assert_eq!(std::fs::metadata(&volume).unwrap().len(), good_len);
        assert_eq!(read_all(&hs, &digest).await, good);
    }

    #[tokio::test]
    async fn rebuild_accepts_uppercase_hex_digest_in_header() {
        let root = TempDir::new().unwrap();
        let volumes = root.path().join("volumes");
        std::fs::create_dir_all(&volumes).unwrap();

        let bytes = b"case-fold";
        let digest = digest_of(bytes);
        let upper = Digest(digest.as_str().to_ascii_uppercase());
        write_needle(&volume_dat(&volumes, vid(9)), &upper, bytes);

        let hs = Haystack::open(root.path()).unwrap();
        // parse normalizes to lowercase — matches the digest clients look up.
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
    }

    #[tokio::test]
    async fn remove_survives_reopen_via_durable_tombstone() {
        let root = TempDir::new().unwrap();
        let bytes = b"index-only-delete";
        let digest = digest_of(bytes);

        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            hs.remove(&digest).await.unwrap();
            assert!(!hs.contains(&digest));
            assert_eq!(
                hs.scan_occupancy().unwrap(),
                (1, expected_occupancy(&[bytes.len()]))
            );
        }

        let hs = Haystack::open(root.path()).unwrap();
        assert!(
            !hs.contains(&digest),
            "WAL delete must keep the digest unservable after reopen"
        );
        assert!(matches!(
            hs.open_blob(&digest).await,
            Err(AppError::NoSuchKey)
        ));
        hs.checkpoint().unwrap();
        let file: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert!(file.entries.iter().any(|e| e.digest == digest && e.deleted));
        assert_eq!(
            hs.scan_occupancy().unwrap(),
            (1, expected_occupancy(&[bytes.len()]))
        );
    }

    #[tokio::test]
    async fn quarantine_refuses_open_but_keeps_index_row() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"bit-rot";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
            .await
            .unwrap();

        assert!(hs.mark_quarantined(&digest).unwrap());
        assert!(!hs.contains(&digest));
        let err = hs.open_blob(&digest).await.unwrap_err();
        assert!(
            matches!(err, AppError::Other(_)),
            "quarantine should not look like a plain miss: {err:?}"
        );
        assert!(!hs.mark_quarantined(&digest).unwrap());

        hs.checkpoint().unwrap();
        let file: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert_eq!(file.entries.len(), 1);
        assert!(file.entries[0].quarantined);
        assert!(!file.entries[0].deleted);
    }

    #[tokio::test]
    async fn scrub_once_quarantines_flipped_payload_byte() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let bytes = b"scrub-me-please";
        let digest = digest_of(bytes);
        hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
            .await
            .unwrap();

        let locator = hs.locate(&digest).unwrap();
        let volume = hs.volume_path(locator.volume_id);
        let mut raw = tokio::fs::read(&volume).await.unwrap();
        let idx = locator.payload_offset as usize;
        raw[idx] ^= 0x01;
        tokio::fs::write(&volume, &raw).await.unwrap();

        assert_eq!(hs.scrub_once().await.unwrap(), 1);
        assert!(!hs.contains(&digest));
        assert!(
            matches!(hs.open_blob(&digest).await.unwrap_err(), AppError::Other(_)),
            "corrupt needle must be refused after scrub"
        );

        // Already quarantined — not re-examined.
        assert_eq!(hs.scrub_once().await.unwrap(), 0);
    }

    #[tokio::test]
    async fn compaction_drops_tombstoned_bytes_and_keeps_live() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let keep = digest_of(b"keep-me");
        let drop = digest_of(b"drop-me");
        hs.commit_temp(&stage(root.path(), "k.tmp", b"keep-me").await, &keep)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "d.tmp", b"drop-me").await, &drop)
            .await
            .unwrap();

        let before = hs.scan_occupancy().unwrap().1;
        assert!(hs.remove(&drop).await.unwrap().is_some());
        assert_eq!(
            hs.scan_occupancy().unwrap().1,
            before,
            "tombstone keeps bytes"
        );

        hs.compaction().await.unwrap();

        assert!(hs.contains(&keep));
        assert!(!hs.contains(&drop));
        assert_eq!(read_all(&hs, &keep).await, b"keep-me");
        let after = hs.scan_occupancy().unwrap().1;
        assert!(
            after < before,
            "compaction should reclaim tombstone bytes: before={before} after={after}"
        );
        assert_eq!(after, expected_occupancy(&[b"keep-me".len()]));

        // Dead row gone from the durable snapshot.
        let file: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert_eq!(file.entries.len(), 1);
        assert_eq!(file.entries[0].digest, keep);
        assert!(!file.entries[0].deleted);
    }

    #[tokio::test]
    async fn compaction_writes_a_new_volume_id_and_unlinks_the_old() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let keep = digest_of(b"keep-me");
        let drop = digest_of(b"drop-me");
        hs.commit_temp(&stage(root.path(), "k.tmp", b"keep-me").await, &keep)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "d.tmp", b"drop-me").await, &drop)
            .await
            .unwrap();

        let old_id = hs.locate(&keep).unwrap().volume_id;
        assert!(hs.volume_path(old_id).exists());

        hs.remove(&drop).await.unwrap();
        hs.compaction().await.unwrap();

        let new_loc = hs.locate(&keep).unwrap();
        assert_ne!(
            new_loc.volume_id, old_id,
            "live needles must move to a new volume id"
        );
        assert!(
            hs.volume_path(new_loc.volume_id).exists(),
            "compacted volume must exist"
        );
        assert!(
            !hs.volume_path(old_id).exists(),
            "old volume must be unlinked after remap"
        );
        assert_eq!(read_all(&hs, &keep).await, b"keep-me");
        assert_eq!(hs.scan_occupancy().unwrap().0, 1);
    }

    #[tokio::test]
    async fn compaction_is_noop_when_nothing_is_dirty() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let a = digest_of(b"aaa");
        let b = digest_of(b"bbb");
        hs.commit_temp(&stage(root.path(), "a.tmp", b"aaa").await, &a)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "b.tmp", b"bbb").await, &b)
            .await
            .unwrap();

        let loc_a = hs.locate(&a).unwrap();
        let loc_b = hs.locate(&b).unwrap();
        let before = hs.scan_occupancy().unwrap();

        hs.compaction().await.unwrap();

        assert_eq!(hs.locate(&a), Some(loc_a), "offsets must not move");
        assert_eq!(hs.locate(&b), Some(loc_b));
        assert_eq!(hs.scan_occupancy().unwrap(), before);
        assert_eq!(read_all(&hs, &a).await, b"aaa");
        assert_eq!(read_all(&hs, &b).await, b"bbb");
    }

    #[tokio::test]
    async fn compaction_deletes_volume_when_all_needles_are_dead() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let digest = digest_of(b"all-dead");
        hs.commit_temp(&stage(root.path(), "t.tmp", b"all-dead").await, &digest)
            .await
            .unwrap();
        let old_id = hs.locate(&digest).unwrap().volume_id;
        assert_eq!(hs.scan_occupancy().unwrap().0, 1);

        hs.remove(&digest).await.unwrap();
        hs.compaction().await.unwrap();

        assert_eq!(hs.scan_occupancy().unwrap(), (0, 0));
        assert!(!hs.volume_path(old_id).exists());
        let file: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert!(file.entries.is_empty());
    }

    #[tokio::test]
    async fn compaction_reclaims_quarantined_needles() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let keep = digest_of(b"good");
        let bad = digest_of(b"bad");
        hs.commit_temp(&stage(root.path(), "g.tmp", b"good").await, &keep)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "b.tmp", b"bad").await, &bad)
            .await
            .unwrap();

        assert!(hs.mark_quarantined(&bad).unwrap());
        let before = hs.scan_occupancy().unwrap().1;

        hs.compaction().await.unwrap();

        assert!(hs.contains(&keep));
        assert!(!hs.contains(&bad));
        assert_eq!(read_all(&hs, &keep).await, b"good");
        assert_eq!(
            hs.scan_occupancy().unwrap().1,
            expected_occupancy(&[b"good".len()])
        );
        assert!(hs.scan_occupancy().unwrap().1 < before);
    }

    #[tokio::test]
    async fn compaction_keeps_neighbors_when_middle_is_tombstoned() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let left = digest_of(b"left");
        let mid = digest_of(b"mid");
        let right = digest_of(b"right");
        hs.commit_temp(&stage(root.path(), "l.tmp", b"left").await, &left)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "m.tmp", b"mid").await, &mid)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "r.tmp", b"right").await, &right)
            .await
            .unwrap();

        hs.remove(&mid).await.unwrap();
        hs.compaction().await.unwrap();

        assert_eq!(read_all(&hs, &left).await, b"left");
        assert_eq!(read_all(&hs, &right).await, b"right");
        assert!(!hs.contains(&mid));
        assert_eq!(
            hs.scan_occupancy().unwrap().1,
            expected_occupancy(&[b"left".len(), b"right".len()])
        );
        // Live needles should be packed at the start of the volume.
        assert_eq!(hs.locate(&left).unwrap().payload_offset, NEEDLE_HEADER_LEN);
        assert_eq!(
            hs.locate(&right).unwrap().payload_offset,
            Haystack::needle_len(b"left".len() as u64) + NEEDLE_HEADER_LEN
        );
    }

    #[tokio::test]
    async fn compaction_survives_reopen() {
        let root = TempDir::new().unwrap();
        let keep = digest_of(b"survive");
        let drop = digest_of(b"gone");
        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "k.tmp", b"survive").await, &keep)
                .await
                .unwrap();
            hs.commit_temp(&stage(root.path(), "d.tmp", b"gone").await, &drop)
                .await
                .unwrap();
            hs.remove(&drop).await.unwrap();
            hs.compaction().await.unwrap();
        }

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&keep));
        assert!(!hs.contains(&drop));
        assert_eq!(read_all(&hs, &keep).await, b"survive");
        assert_eq!(
            hs.scan_occupancy().unwrap().1,
            expected_occupancy(&[b"survive".len()])
        );
    }

    #[tokio::test]
    async fn compaction_allows_further_commits() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let first = digest_of(b"first");
        let dead = digest_of(b"dead");
        hs.commit_temp(&stage(root.path(), "f.tmp", b"first").await, &first)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "d.tmp", b"dead").await, &dead)
            .await
            .unwrap();
        hs.remove(&dead).await.unwrap();
        hs.compaction().await.unwrap();

        let second = digest_of(b"second");
        hs.commit_temp(&stage(root.path(), "s.tmp", b"second").await, &second)
            .await
            .unwrap();

        assert_eq!(read_all(&hs, &first).await, b"first");
        assert_eq!(read_all(&hs, &second).await, b"second");
        assert_eq!(
            hs.scan_occupancy().unwrap().1,
            expected_occupancy(&[b"first".len(), b"second".len()])
        );
    }

    #[tokio::test]
    async fn compaction_second_pass_is_noop() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let keep = digest_of(b"once");
        let drop = digest_of(b"twice");
        hs.commit_temp(&stage(root.path(), "k.tmp", b"once").await, &keep)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "d.tmp", b"twice").await, &drop)
            .await
            .unwrap();
        hs.remove(&drop).await.unwrap();
        hs.compaction().await.unwrap();

        let loc = hs.locate(&keep).unwrap();
        let occ = hs.scan_occupancy().unwrap();
        hs.compaction().await.unwrap();
        assert_eq!(hs.locate(&keep), Some(loc));
        assert_eq!(hs.scan_occupancy().unwrap(), occ);
        assert_eq!(read_all(&hs, &keep).await, b"once");
    }

    #[tokio::test]
    async fn scrub_once_leaves_intact_needles_openable() {
        let root = TempDir::new().unwrap();
        let hs = Haystack::open(root.path()).unwrap();
        let a = digest_of(b"aaa");
        let b = digest_of(b"bbb");
        hs.commit_temp(&stage(root.path(), "a.tmp", b"aaa").await, &a)
            .await
            .unwrap();
        hs.commit_temp(&stage(root.path(), "b.tmp", b"bbb").await, &b)
            .await
            .unwrap();

        assert_eq!(hs.scrub_once().await.unwrap(), 2);
        assert!(hs.contains(&a));
        assert!(hs.contains(&b));

        let mut got = Vec::new();
        hs.open_blob(&a)
            .await
            .unwrap()
            .read_to_end(&mut got)
            .await
            .unwrap();
        assert_eq!(got, b"aaa");
    }

    #[tokio::test]
    async fn durable_index_round_trips_locators_across_reopen() {
        let root = TempDir::new().unwrap();
        let a = b"aaa";
        let b = b"bbbb";
        let da = digest_of(a);
        let db = digest_of(b);
        let (loc_a, loc_b) = {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "a.tmp", a).await, &da)
                .await
                .unwrap();
            hs.commit_temp(&stage(root.path(), "b.tmp", b).await, &db)
                .await
                .unwrap();
            (hs.locate(&da).unwrap(), hs.locate(&db).unwrap())
        };

        let hs = Haystack::open(root.path()).unwrap();
        assert_eq!(hs.locate(&da), Some(loc_a));
        assert_eq!(hs.locate(&db), Some(loc_b));
        assert_eq!(read_all(&hs, &da).await, a);
        assert_eq!(read_all(&hs, &db).await, b);
    }

    #[tokio::test]
    async fn corrupt_snapshot_still_replays_wal() {
        let root = TempDir::new().unwrap();
        let bytes = b"recover-me";
        let digest = digest_of(bytes);
        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            // Leave the put in the WAL; do not checkpoint.
        }
        std::fs::write(
            root.path().join("volumes").join(INDEX_FILE),
            b"not-an-index",
        )
        .unwrap();

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
    }

    #[tokio::test]
    async fn missing_snapshot_and_wal_recovers_via_volume_scan() {
        let root = TempDir::new().unwrap();
        let bytes = b"recover-me";
        let digest = digest_of(bytes);
        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            hs.checkpoint().unwrap();
        }
        std::fs::remove_file(root.path().join("volumes").join(INDEX_FILE)).unwrap();
        let _ = std::fs::remove_file(root.path().join("volumes").join(LOG_FILE));

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
        let rewritten: NeedleIndexFile =
            serde_json::from_slice(&std::fs::read(hs.index_path()).unwrap()).unwrap();
        assert_eq!(rewritten.version, INDEX_VERSION);
        assert_eq!(rewritten.entries.len(), 1);
        assert_eq!(rewritten.entries[0].digest, digest);
    }

    #[tokio::test]
    async fn wal_survives_reopen_without_checkpoint() {
        let root = TempDir::new().unwrap();
        let bytes = b"wal-only";
        let digest = digest_of(bytes);
        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            assert!(hs.is_index_dirty());
            // No checkpoint — only needles.log has the put.
        }
        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
    }

    #[tokio::test]
    async fn concurrent_commits_to_distinct_digests() {
        let root = TempDir::new().unwrap();
        let hs = std::sync::Arc::new(Haystack::open(root.path()).unwrap());

        let payloads: Vec<Vec<u8>> = (0..8)
            .map(|i| format!("payload-{i}").into_bytes())
            .collect();
        let digests: Vec<Digest> = payloads.iter().map(|p| digest_of(p)).collect();

        let mut handles = Vec::new();
        for (i, (payload, digest)) in payloads.iter().zip(digests.iter()).enumerate() {
            let hs = hs.clone();
            let root = root.path().to_path_buf();
            let payload = payload.clone();
            let digest = digest.clone();
            handles.push(tokio::spawn(async move {
                let temp = stage(&root, &format!("c{i}.tmp"), &payload).await;
                hs.commit_temp(&temp, &digest).await.unwrap();
            }));
        }
        for handle in handles {
            handle.await.unwrap();
        }

        for (payload, digest) in payloads.iter().zip(digests.iter()) {
            assert!(hs.contains(digest));
            assert_eq!(read_all(&hs, digest).await, payload.as_slice());
        }
    }
}
