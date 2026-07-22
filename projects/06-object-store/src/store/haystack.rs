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
//! [`NeedleLocator::offset`] is the **payload** start (after the header) so GET
//! can seek + `take(size)` without re-parsing. Sync [`Self::rebuild_index`] at
//! open recovers the map by scanning every `*.dat` **in parallel** (one thread
//! per volume via [`std::thread::scope`]) and truncates a torn tail.
//!
//! ## Scaffold status
//!
//! Commit, open (capped), remove-from-index, and rebuild-on-open are implemented.
//! Compaction / scrub bodies are still `todo!()`. Default store boot stays on
//! [`BlobLayoutKind::FileCas`](super::BlobLayoutKind::FileCas);
//! select this layout with `BLOB_LAYOUT=haystack`.

use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom as SyncSeekFrom};
use std::path::{Path, PathBuf};

use tokio::fs::OpenOptions;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt, Take};

use crate::error::AppError;
use crate::object::Digest;

const MAX_VOLUME_SIZE: u64 = 1024 * 1024;
const VOLUME_EXT: &str = "dat";
/// Hex digest (`Digest::LEN`) + little-endian `u64` payload length.
const NEEDLE_HEADER_LEN: u64 = Digest::LEN as u64 + std::mem::size_of::<u64>() as u64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct VolumeId(u32);

impl VolumeId {
    const ZERO: Self = Self(0);

    fn next(self) -> Self {
        Self(self.0.saturating_add(1))
    }

    fn parse(name: &std::ffi::OsStr) -> Option<Self> {
        let name = name.to_str()?;
        let stem = name.strip_suffix(".dat")?;
        stem.parse().ok().map(Self)
    }
}

impl std::fmt::Display for VolumeId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}

/// Where a committed needle's **payload** lives inside a volume file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NeedleLocator {
    pub volume_id: VolumeId,
    pub offset: u64,
    pub size: u64,
}

#[derive(Debug)]
struct ActiveFile {
    volume_id: VolumeId,
    file: tokio::fs::File,
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
    volumes_dir: PathBuf,
    index: parking_lot::Mutex<HashMap<Digest, NeedleLocator>>,
    active_file: tokio::sync::Mutex<Option<ActiveFile>>,
}

impl Haystack {
    /// Create `volumes/` under `root` and rebuild the needle index from disk.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the directory cannot be created or a volume
    /// cannot be scanned / truncated.
    pub fn open(root: impl AsRef<Path>) -> std::io::Result<Self> {
        let volumes_dir = root.as_ref().join("volumes");
        std::fs::create_dir_all(&volumes_dir)?;

        let haystack = Self {
            volumes_dir,
            index: parking_lot::Mutex::new(HashMap::new()),
            active_file: tokio::sync::Mutex::new(None),
        };
        haystack.rebuild_index()?;
        Ok(haystack)
    }

    fn volume_path(&self, volume_id: VolumeId) -> PathBuf {
        self.volumes_dir.join(format!("{volume_id}.{VOLUME_EXT}"))
    }

    fn needle_len(payload_len: u64) -> u64 {
        NEEDLE_HEADER_LEN + payload_len
    }

    /// Ensure `active_file` is an open, writable volume with room for `size_to_append`.
    async fn ensure_active_file(
        &self,
        active: &mut Option<ActiveFile>,
        size_to_append: u64,
    ) -> Result<(), AppError> {
        if let Some(file) = active.as_ref() {
            if file.current_offset + size_to_append <= MAX_VOLUME_SIZE {
                return Ok(());
            }
        }

        // Prefer an existing volume that still has room.
        let mut next_id = VolumeId::ZERO;
        let mut candidate: Option<(VolumeId, u64)> = None;
        let mut entries = tokio::fs::read_dir(&self.volumes_dir).await?;
        while let Some(entry) = entries.next_entry().await? {
            let Some(volume_id) = VolumeId::parse(&entry.file_name()) else {
                continue;
            };
            next_id = next_id.max(volume_id.next());
            let metadata = entry.metadata().await?;
            if metadata.is_file()
                && metadata.len() + size_to_append <= MAX_VOLUME_SIZE
                && candidate.is_none()
            {
                candidate = Some((volume_id, metadata.len()));
            }
        }

        if let Some((volume_id, len)) = candidate {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .open(self.volume_path(volume_id))
                .await?;
            *active = Some(ActiveFile::new(volume_id, file, len));
            return Ok(());
        }

        let volume_id = next_id;
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
    pub fn scan_occupancy(&self) -> std::io::Result<(u64, u64)> {
        let mut file_count = 0u64;
        let mut total_bytes = 0u64;
        for entry in std::fs::read_dir(&self.volumes_dir)? {
            let entry = entry?;
            let metadata = entry.metadata()?;
            if metadata.is_file() {
                file_count += 1;
                total_bytes += metadata.len();
            }
        }
        Ok((file_count, total_bytes))
    }

    /// Dedup check against the in-memory needle map (not the filesystem tree).
    pub fn contains(&self, digest: &Digest) -> bool {
        self.index.lock().contains_key(digest)
    }

    /// Look up a needle locator if the digest was committed into a volume.
    pub fn locate(&self, digest: &Digest) -> Option<NeedleLocator> {
        self.index.lock().get(digest).copied()
    }

    /// Append `temp`'s bytes as a framed needle and index `digest`.
    ///
    /// Durability: header + payload are `sync_all`'d into the volume before the
    /// in-memory index advertises the digest. The staging `temp` is removed on
    /// success (callers disarm their [`crate::durable::TempEntry`] afterward).
    ///
    /// # Errors
    ///
    /// Returns an I/O-backed [`AppError`] if the volume cannot be opened,
    /// appended, synced, or if `temp` cannot be removed.
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
            offset: payload_offset,
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

        self.index.lock().insert(digest.clone(), locator);
        drop(guard);

        tokio::fs::remove_file(temp).await?;
        Ok(())
    }

    /// Open needle **payload** bytes for `digest`, capped at the locator size.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] if the digest is not in the needle map,
    /// or an I/O-backed [`AppError`] if the volume cannot be opened / seeked.
    pub async fn open_blob(&self, digest: &Digest) -> Result<Take<tokio::fs::File>, AppError> {
        let locator = self.locate(digest).ok_or(AppError::NoSuchKey)?;
        let mut file = tokio::fs::File::open(self.volume_path(locator.volume_id)).await?;
        file.seek(std::io::SeekFrom::Start(locator.offset)).await?;
        Ok(file.take(locator.size))
    }

    /// Open an inclusive byte range within a needle's payload.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] if missing, [`AppError::InvalidRequest`]
    /// if the range is outside the payload, or an I/O-backed [`AppError`].
    pub async fn open_blob_range(
        &self,
        digest: &Digest,
        start: u64,
        end: u64,
    ) -> Result<Take<tokio::fs::File>, AppError> {
        let locator = self.locate(digest).ok_or(AppError::NoSuchKey)?;
        if start > end || end >= locator.size {
            return Err(AppError::InvalidRequest(format!(
                "invalid range: start={start} end={end} needle_size={}",
                locator.size
            )));
        }
        let mut file = tokio::fs::File::open(self.volume_path(locator.volume_id)).await?;
        file.seek(std::io::SeekFrom::Start(locator.offset + start))
            .await?;
        Ok(file.take(end - start + 1))
    }

    /// Drop a digest from the needle map; physical reclaim is compaction (lab).
    ///
    /// Idempotent: missing digests return `Ok(None)`. Volume bytes are left in
    /// place until a later compaction pass.
    pub async fn remove(&self, digest: &Digest) -> Result<Option<u64>, AppError> {
        Ok(self.index.lock().remove(digest).map(|loc| loc.size))
    }

    /// Re-hash every live needle; quarantine digests whose payload ≠ name.
    ///
    /// # Panics
    ///
    /// Scaffold: panics via `todo!` — scrubbing shifts from `rehash(file)==path`
    /// to `rehash(needle)==digest` (see docs/11 §4).
    pub async fn scrub_once(&self) -> Result<u64, AppError> {
        todo!(
            "haystack: walk index (or volumes), rehash each needle payload, quarantine mismatches"
        )
    }

    /// Scan every `*.dat` volume and rebuild the in-memory needle map.
    ///
    /// Each volume is scanned on its own thread ([`std::thread::scope`]) so
    /// multi-volume boots scale with disk parallelism. Within one volume the
    /// scan stays sequential (needles are a linked byte stream). Incomplete
    /// trailing needles are not indexed; that volume is truncated to the last
    /// complete needle. If the same digest appears in two volumes (should not
    /// happen under normal dedup), a later merge wins — order is unspecified.
    fn rebuild_index(&self) -> std::io::Result<()> {
        let volumes = {
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
            volumes
        };
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

    fn scan_volume(
        volume_id: VolumeId,
        path: &Path,
    ) -> std::io::Result<HashMap<Digest, NeedleLocator>> {
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
                NeedleLocator {
                    volume_id,
                    offset: payload_pos,
                    size,
                },
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
    use sha2::{Digest as _, Sha256};
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

    fn digest_of(bytes: &[u8]) -> Digest {
        Digest(hex::encode(Sha256::digest(bytes)))
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
        assert_eq!(
            locator,
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: NEEDLE_HEADER_LEN,
                size: bytes.len() as u64,
            }
        );
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
        assert_eq!(
            hs.locate(&da).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: a_payload,
                size: 3,
            }
        );
        assert_eq!(
            hs.locate(&db).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: b_payload,
                size: 4,
            }
        );
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
        assert_eq!(
            hs.locate(&digest).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: NEEDLE_HEADER_LEN,
                size: bytes.len() as u64,
            }
        );
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

        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
        }

        let volume = root.path().join("volumes").join("0.dat");
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

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(std::fs::metadata(&volume).unwrap().len(), good_len);
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

        write_needle(&volumes.join("0.dat"), &da, a);
        write_needle(&volumes.join("1.dat"), &db, b);

        let hs = Haystack::open(root.path()).unwrap();
        assert_eq!(
            hs.locate(&da).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: NEEDLE_HEADER_LEN,
                size: a.len() as u64,
            }
        );
        assert_eq!(
            hs.locate(&db).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(1),
                offset: NEEDLE_HEADER_LEN,
                size: b.len() as u64,
            }
        );

        assert_eq!(read_all(&hs, &da).await, a);
        assert_eq!(read_all(&hs, &db).await, b);
    }

    #[test]
    fn volume_id_parse_accepts_dat_stems_and_rejects_junk() {
        assert_eq!(
            VolumeId::parse(std::ffi::OsStr::new("0.dat")),
            Some(VolumeId(0))
        );
        assert_eq!(
            VolumeId::parse(std::ffi::OsStr::new("42.dat")),
            Some(VolumeId(42))
        );
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new("0.idx")), None);
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new("notes.txt")), None);
        assert_eq!(VolumeId::parse(std::ffi::OsStr::new(".dat")), None);
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
    async fn remove_unindexes_but_leaves_volume_bytes() {
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
            hs.locate(&digest).unwrap(),
            NeedleLocator {
                volume_id: VolumeId(0),
                offset: NEEDLE_HEADER_LEN,
                size: 0,
            }
        );
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

        let big_len = (MAX_VOLUME_SIZE - NEEDLE_HEADER_LEN) as usize;
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

        assert_eq!(hs.locate(&d_big).unwrap().volume_id, VolumeId(0));
        assert_eq!(hs.locate(&d_small).unwrap().volume_id, VolumeId(1));
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
        write_needle(&volumes.join("0.dat"), &digest, bytes);
        std::fs::write(volumes.join("notes.txt"), b"not a volume").unwrap();
        std::fs::write(volumes.join("0.idx"), b"sidecar").unwrap();

        let hs = Haystack::open(root.path()).unwrap();
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
        // Junk files still count toward occupancy's file walk.
        assert_eq!(hs.scan_occupancy().unwrap().0, 3);
    }

    #[tokio::test]
    async fn rebuild_truncates_torn_payload_after_valid_header() {
        let root = TempDir::new().unwrap();
        let volumes = root.path().join("volumes");
        std::fs::create_dir_all(&volumes).unwrap();

        let good = b"complete-needle";
        let digest = digest_of(good);
        let volume = volumes.join("0.dat");
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
        write_needle(&volumes.join("0.dat"), &upper, bytes);

        let hs = Haystack::open(root.path()).unwrap();
        // parse normalizes to lowercase — matches the digest clients look up.
        assert!(hs.contains(&digest));
        assert_eq!(read_all(&hs, &digest).await, bytes);
    }

    #[tokio::test]
    async fn index_only_remove_is_undone_by_rebuild_until_compaction() {
        let root = TempDir::new().unwrap();
        let bytes = b"index-only-delete";
        let digest = digest_of(bytes);

        {
            let hs = Haystack::open(root.path()).unwrap();
            hs.commit_temp(&stage(root.path(), "t.tmp", bytes).await, &digest)
                .await
                .unwrap();
            hs.remove(&digest).await.unwrap();
        }

        // Rebuild from disk brings the needle back — remove is index-only until
        // compaction. That is intentional; assert the current contract.
        let hs = Haystack::open(root.path()).unwrap();
        assert!(
            hs.contains(&digest),
            "rebuild re-discovers needles still on disk after index-only remove"
        );
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
