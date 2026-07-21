//! V1 — The content-addressed blob store (CAS): the durable, dedup'd foundation.
//!
//! This is the layer you'd normally get from S3/MinIO. Every distinct piece of
//! content lives exactly once under `objects/`, named by the SHA-256 of its
//! bytes. V1 owns only "given finished bytes and their digest, store them safely
//! and idempotently" — the `(bucket,key) → digest` mapping is V3's job.
//!
//! The trap is the durable write. You cannot write straight to `objects/<hash>`:
//! a crash mid-write leaves a file with the right name but truncated contents,
//! and every future reader trusts it. The fix is the temp→fsync→rename→fsync-dir
//! dance — `rename` within a filesystem is atomic, so the final name only ever
//! appears once the bytes are fully there.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sha2::{Digest as _, Sha256};
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tracing::{error, info, warn};

use crate::error::AppError;
use crate::object::Digest;

/// Store committed blobs and in-flight writes in separate on-disk trees.
///
/// Owns the `objects/` tree (committed, content-named
/// blobs) and the `tmp/` tree (in-flight writes, before they're atomically
/// renamed into place). A background [`Scrubber`] re-hashes committed blobs
/// and refuses to open any that fail the content-address check.
pub struct Store {
    objects: PathBuf,
    tmp: PathBuf,
    scrubber: Scrubber,
}

impl Store {
    /// Open the blob store under `root`, creating its directory trees if needed.
    ///
    /// Returns the store behind an [`Arc`] because request handlers and the
    /// index share the same filesystem layout.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if either the `objects/` or `tmp/` directory cannot
    /// be created.
    pub fn open(root: impl AsRef<Path>) -> std::io::Result<Arc<Self>> {
        let root = root.as_ref();
        let objects = {
            let objects = root.join("objects");
            std::fs::create_dir_all(&objects)?;
            objects
        };
        let tmp = {
            let tmp = root.join("tmp");
            std::fs::create_dir_all(&tmp)?;
            tmp
        };
        let quarantine = {
            let quarantine = root.join("quarantine");
            std::fs::create_dir_all(&quarantine)?;
            quarantine
        };
        let (blob_count, total_bytes) = Self::scan_occupancy(&objects)?;
        metrics::gauge!(crate::metrics::BLOB_COUNT).set(blob_count as f64);
        metrics::gauge!(crate::metrics::TOTAL_BYTES_STORED).set(total_bytes as f64);
        Ok(Arc::new(Self {
            objects,
            tmp,
            scrubber: Scrubber::new(quarantine),
        }))
    }

    fn scan_occupancy(objects: &Path) -> std::io::Result<(u64, u64)> {
        let mut blob_count = 0u64;
        let mut total_bytes = 0u64;
        let mut stack = vec![objects.to_path_buf()];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(dir)? {
                let entry = entry?;
                let metadata = entry.metadata()?;
                if metadata.is_dir() {
                    stack.push(entry.path());
                } else if metadata.is_file() {
                    blob_count += 1;
                    total_bytes += metadata.len();
                }
            }
        }
        Ok((blob_count, total_bytes))
    }

    /// Return the directory where V2 stages in-flight writes.
    ///
    /// A temp file here
    /// is renamed onto its final `blob_path` only once fully written + fsync'd.
    /// Pair with [`TempEntry::unique_in`](crate::durable::TempEntry::unique_in)
    /// to stage a guarded path in this tree.
    pub fn tmp_dir(&self) -> &Path {
        &self.tmp
    }

    /// Map a digest to its sharded on-disk blob path.
    ///
    /// Paths are fanned out by the leading hash bytes
    /// (`objects/ab/cd/abcd…`) so no single directory holds millions of entries.
    ///
    /// # Panics
    ///
    /// Panics if the digest is shorter than four bytes or its first four byte
    /// offsets are not UTF-8 character boundaries. Valid hex digests satisfy
    /// both requirements.
    pub fn blob_path(&self, digest: &Digest) -> PathBuf {
        self.objects
            .join(&digest.as_str()[0..2])
            .join(&digest.as_str()[2..4])
            .join(digest.as_str())
    }

    /// Recover a digest from a path in the sharded blob layout.
    ///
    /// This is the inverse of [`Self::blob_path`] for a path under
    /// `objects/` when it matches the sharded layout (`ab/cd/<64-hex>`).
    /// Returns `None` for paths outside the blob tree or with the wrong shape.
    pub fn digest_from_path(&self, path: &Path) -> Option<Digest> {
        let rel = path.strip_prefix(&self.objects).ok()?;
        let parts: Vec<&str> = rel
            .components()
            .map(|c| c.as_os_str().to_str())
            .collect::<Option<_>>()?;

        let [shard_a, shard_b, name] = parts.as_slice() else {
            return None;
        };
        if shard_a.len() != 2 || shard_b.len() != 2 || name.len() != 64 {
            return None;
        }
        if !name.chars().all(|c| c.is_ascii_hexdigit()) {
            return None;
        }
        Some(Digest(name.to_string()))
    }

    /// Report whether a committed blob exists for the digest.
    ///
    /// This is the dedup check —
    /// if it does, identical bytes are already stored and we skip the write.
    /// Filesystem lookup errors are treated as “not found”.
    pub async fn contains(&self, digest: &Digest) -> bool {
        tokio::fs::try_exists(self.blob_path(digest))
            .await
            .unwrap_or(false)
    }

    /// Commit a fully written temporary file durably and atomically.
    ///
    /// This is the heart of V1: the file is synced, renamed to the
    /// content-addressed path, and followed by a directory sync. If the digest
    /// already exists, the duplicate temporary file is removed.
    ///
    /// # Errors
    ///
    /// Returns an [`AppError`] if the temporary file cannot be removed or
    /// synced, the destination tree cannot be created, the rename fails, or
    /// the destination directory cannot be synced.
    ///
    /// # Panics
    ///
    /// Panics if the path produced by [`Self::blob_path`] has no parent.
    pub async fn commit_temp(&self, temp: &Path, digest: &Digest) -> Result<(), AppError> {
        if self.contains(digest).await {
            tokio::fs::remove_file(temp).await?;
            metrics::counter!(crate::metrics::DEDUP_HITS_TOTAL).increment(1);
            return Ok(());
        }
        let size = tokio::fs::metadata(temp).await?.len();
        let blob_path = self.blob_path(digest);
        crate::durable::publish_temp(temp, &blob_path).await?;
        metrics::gauge!(crate::metrics::BLOB_COUNT).increment(1.0);
        metrics::gauge!(crate::metrics::TOTAL_BYTES_STORED).increment(size as f64);
        // Wake an idle scrubber that parked because `objects/` was empty.
        self.scrubber.notify.notify_one();
        Ok(())
    }

    /// Stage in-memory `bytes` under `tmp/` and [`commit_temp`] them at their
    /// SHA-256 content address.
    ///
    /// Convenience for callers that already hold the full blob (CDC chunks,
    /// manifest JSON) rather than streaming into a temp file themselves.
    /// Dedup, metrics, and scrubber wake all go through [`Self::commit_temp`].
    ///
    /// # Errors
    ///
    /// Propagates I/O errors staging the temp file or from [`Self::commit_temp`].
    pub async fn commit_bytes(&self, bytes: &[u8]) -> Result<Digest, AppError> {
        let digest = Digest(hex::encode(Sha256::digest(bytes)));
        let mut temp = crate::durable::TempEntry::unique_in(self.tmp_dir(), "bytes");
        tokio::fs::write(temp.path(), bytes).await?;
        self.commit_temp(temp.path(), &digest).await?;
        temp.disarm();
        Ok(digest)
    }

    /// Open a committed blob for asynchronous reading.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] when the digest is not committed, an
    /// integrity error when the scrubber has quarantined the digest, or an
    /// I/O-backed [`AppError`] when checking or opening the file fails.
    pub async fn open_blob(&self, digest: &Digest) -> Result<tokio::fs::File, AppError> {
        if self.scrubber.is_quarantined(digest) {
            return Err(AppError::Other(anyhow::anyhow!(
                "blob failed integrity check"
            )));
        }
        let path = self.blob_path(digest);
        if !tokio::fs::try_exists(&path).await? {
            return Err(AppError::NoSuchKey);
        }

        Ok(tokio::fs::File::open(path).await?)
    }

    /// Spawn the continuous scrubber.
    ///
    /// When the blob tree is empty the task parks on [`Notify`] (no busy loop)
    /// until [`Self::commit_temp`] wakes it. When blobs exist it re-scans on
    /// `rescan_interval`, waking early when a new blob is committed.
    pub fn spawn_scrubber(
        self: Arc<Self>,
        rescan_interval: Duration,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            self.run_scrubber(rescan_interval).await;
        })
    }

    async fn run_scrubber(&self, rescan_interval: Duration) {
        info!(?rescan_interval, "blob scrubber started");
        loop {
            match self.scrub_once().await {
                Ok(0) => {
                    // Nothing to audit — sleep until the next durable commit.
                    metrics::counter!(crate::metrics::SCRUB_IDLE_WAITS_TOTAL).increment(1);
                    self.scrubber.notify.notified().await;
                }
                Ok(_) => {
                    // Re-scrub for bit rot on a timer; a new commit short-circuits.
                    tokio::select! {
                        _ = self.scrubber.notify.notified() => {}
                        _ = tokio::time::sleep(rescan_interval) => {}
                    }
                }
                Err(err) => {
                    error!(error = %err, "scrub pass failed");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        }
    }

    /// One full walk of `objects/`: re-hash every committed blob and quarantine
    /// any whose bytes no longer match their content address.
    ///
    /// Returns the number of blobs examined (verified + corrupted).
    async fn scrub_once(&self) -> Result<u64, AppError> {
        let started = Instant::now();
        let mut stack = vec![self.objects.clone()];
        let mut buffer = vec![0u8; 1024 * 1024];
        let mut examined = 0u64;

        while let Some(dir) = stack.pop() {
            let mut entries = tokio::fs::read_dir(&dir).await?;
            while let Some(entry) = entries.next_entry().await? {
                let file_type = entry.file_type().await?;
                if file_type.is_dir() {
                    stack.push(entry.path());
                    continue;
                }
                if !file_type.is_file() {
                    continue;
                }

                let path = entry.path();
                let Some(digest) = self.digest_from_path(&path) else {
                    continue;
                };
                if self.scrubber.is_quarantined(&digest) {
                    continue;
                }

                examined += 1;
                let mut file = tokio::fs::File::open(&path).await?;
                let mut hasher = Sha256::new();
                let mut bytes_scanned = 0u64;
                loop {
                    let n = file.read(&mut buffer).await?;
                    if n == 0 {
                        break;
                    }
                    bytes_scanned += n as u64;
                    hasher.update(&buffer[..n]);
                }
                metrics::counter!(crate::metrics::SCRUB_BYTES_SCANNED_TOTAL)
                    .increment(bytes_scanned);

                let calculated = Digest(hex::encode(hasher.finalize()));
                if calculated == digest {
                    metrics::counter!(crate::metrics::SCRUB_BLOBS_VERIFIED_TOTAL).increment(1);
                } else {
                    warn!(
                        digest = %digest.as_str(),
                        "scrub detected content-address mismatch; quarantining"
                    );
                    self.quarantine_blob(&path, &digest).await?;
                }
            }
        }

        metrics::counter!(crate::metrics::SCRUB_PASSES_TOTAL).increment(1);
        metrics::histogram!(crate::metrics::SCRUB_PASS_DURATION)
            .record(started.elapsed().as_secs_f64());
        Ok(examined)
    }

    async fn quarantine_blob(&self, path: &Path, digest: &Digest) -> Result<(), AppError> {
        self.scrubber.mark_quarantined(digest);
        let dest = self.scrubber.quarantine_dir.join(digest.as_str());
        match tokio::fs::rename(path, &dest).await {
            Ok(()) => {
                if let Ok(meta) = tokio::fs::metadata(&dest).await {
                    metrics::gauge!(crate::metrics::BLOB_COUNT).decrement(1.0);
                    metrics::gauge!(crate::metrics::TOTAL_BYTES_STORED)
                        .decrement(meta.len() as f64);
                }
            }
            Err(err) => {
                error!(
                    digest = %digest.as_str(),
                    error = %err,
                    "failed to move corrupt blob into quarantine"
                );
            }
        }
        metrics::counter!(crate::metrics::SCRUB_CORRUPTIONS_TOTAL).increment(1);
        Ok(())
    }

    /// Open an inclusive byte range of a committed blob.
    ///
    /// The returned reader starts at `start` and yields at most
    /// `end - start + 1` bytes, allowing [`tokio_util::io::ReaderStream`] to
    /// serve an HTTP range without reading through the rest of the file.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::NoSuchKey`] if the blob is missing,
    /// [`AppError::InvalidRequest`] if `start > end` or `end` is outside the
    /// file, and an I/O-backed [`AppError`] if metadata lookup or seeking fails.
    pub async fn open_blob_range(
        &self,
        digest: &Digest,
        start: u64,
        end: u64,
    ) -> Result<tokio::io::Take<tokio::fs::File>, AppError> {
        let mut file = self.open_blob(digest).await?;
        let file_len = file.metadata().await?.len();

        if start > end || end >= file_len {
            return Err(AppError::InvalidRequest(format!(
                "invalid range: start={start} end={end} file_len={file_len}"
            )));
        }

        file.seek(std::io::SeekFrom::Start(start)).await?;
        let len = end - start + 1;
        Ok(file.take(len))
    }

    /// Return the root directory containing committed blobs.
    pub fn objects_root(&self) -> &Path {
        &self.objects
    }

    /// Remove a committed blob if it exists.
    ///
    /// The operation is idempotent: removing a missing blob succeeds.
    ///
    /// # Errors
    ///
    /// Returns an I/O-backed [`AppError`] if the existence check or removal
    /// fails.
    pub async fn remove(&self, digest: &Digest) -> Result<(), AppError> {
        use tokio::fs as tfs;
        let path = self.blob_path(digest);
        if !tfs::try_exists(&path).await? {
            return Ok(());
        }
        let size = tfs::metadata(&path).await?.len();
        tfs::remove_file(path).await?;
        metrics::gauge!(crate::metrics::BLOB_COUNT).decrement(1.0);
        metrics::gauge!(crate::metrics::TOTAL_BYTES_STORED).decrement(size as f64);
        Ok(())
    }
}

/// Background integrity auditor for the content-addressed blob tree.
///
/// Parks on [`Notify`] when there is nothing to scan (so an empty store does
/// not burn CPU), and tracks digests that failed a re-hash so [`Store::open_blob`]
/// can refuse them without involving the index.
struct Scrubber {
    notify: tokio::sync::Notify,
    quarantined: parking_lot::Mutex<HashSet<Digest>>,
    quarantine_dir: PathBuf,
}

impl Scrubber {
    fn new(quarantine_dir: PathBuf) -> Self {
        Self {
            notify: tokio::sync::Notify::new(),
            quarantined: parking_lot::Mutex::new(HashSet::new()),
            quarantine_dir,
        }
    }

    fn is_quarantined(&self, digest: &Digest) -> bool {
        self.quarantined.lock().contains(digest)
    }

    fn mark_quarantined(&self, digest: &Digest) {
        self.quarantined.lock().insert(digest.clone());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use proptest::test_runner::TestCaseError;
    use sha2::Sha256;
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

    /// Run one async case on a fresh current-thread runtime (`proptest` bodies are sync).
    fn block_on<F: std::future::Future>(fut: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build current-thread runtime")
            .block_on(fut)
    }

    /// Turn any `Debug` error into a `proptest` failure (a shrunk counterexample
    /// beats a bare `unwrap` panic).
    fn fail<E: std::fmt::Debug>(e: E) -> TestCaseError {
        TestCaseError::fail(format!("{e:?}"))
    }

    /// A store rooted in a fresh temp dir, wiped when the `TempDir` drops.
    fn fresh_store() -> (TempDir, Arc<Store>) {
        let root = TempDir::new().expect("create temp root");
        let store = Store::open(root.path()).expect("open store");
        (root, store)
    }

    /// The real content address: hex-encoded SHA-256 of the bytes.
    fn digest_of(bytes: &[u8]) -> Digest {
        Digest(hex::encode(Sha256::digest(bytes)))
    }

    /// Stage a fully-written temp file the way V2 would, ready for commit.
    async fn stage_temp(store: &Store, name: &str, bytes: &[u8]) -> PathBuf {
        let path = store.tmp_dir().join(name);
        tokio::fs::write(&path, bytes).await.expect("write temp");
        path
    }

    /// Stage + commit `bytes` in one step and hand back their content address,
    /// so range tests can start from a committed blob without the two-line dance.
    async fn commit_bytes(store: &Store, bytes: &[u8]) -> Digest {
        let digest = digest_of(bytes);
        let temp = stage_temp(store, &format!("commit-{}", digest.as_str()), bytes).await;
        store.commit_temp(&temp, &digest).await.expect("commit");
        digest
    }

    /// Drain a `Take<File>` (what `open_blob_range` returns) into a `Vec`.
    async fn read_all(mut reader: tokio::io::Take<tokio::fs::File>) -> Vec<u8> {
        let mut out = Vec::new();
        reader.read_to_end(&mut out).await.expect("read range");
        out
    }

    #[test]
    fn open_creates_objects_and_tmp_trees() {
        let (root, store) = fresh_store();
        assert!(root.path().join("objects").is_dir());
        assert!(root.path().join("tmp").is_dir());
        assert_eq!(store.tmp_dir(), root.path().join("tmp"));
    }

    #[test]
    fn blob_path_fans_out_deterministically() {
        let (root, store) = fresh_store();
        let digest = digest_of(b"hello");
        let hex = digest.as_str();

        let path = store.blob_path(&digest);
        assert_eq!(
            path,
            root.path()
                .join("objects")
                .join(&hex[0..2])
                .join(&hex[2..4])
                .join(hex),
        );
        // Same digest → same path, every time; the filename round-trips the digest.
        assert_eq!(path, store.blob_path(&digest));
        assert_eq!(path.file_name().unwrap().to_str().unwrap(), hex);
        assert_eq!(store.digest_from_path(&path), Some(digest));
    }

    #[tokio::test]
    async fn contains_is_false_for_unknown_digest() {
        let (_root, store) = fresh_store();
        assert!(!store.contains(&digest_of(b"never stored")).await);
    }

    #[tokio::test]
    async fn commit_makes_blob_visible_and_consumes_the_temp() {
        let (_root, store) = fresh_store();
        let bytes = b"the quick brown fox";
        let digest = digest_of(bytes);
        let temp = stage_temp(&store, "upload-1", bytes).await;

        store.commit_temp(&temp, &digest).await.expect("commit");

        assert!(store.contains(&digest).await);
        let stored = tokio::fs::read(store.blob_path(&digest)).await.unwrap();
        assert_eq!(stored, bytes);
        // The temp file was renamed into place, not copied — nothing left behind.
        assert!(!tokio::fs::try_exists(&temp).await.unwrap());
    }

    #[tokio::test]
    async fn committing_the_same_bytes_twice_dedups_to_one_blob() {
        let (_root, store) = fresh_store();
        let bytes = b"stored exactly once";
        let digest = digest_of(bytes);

        let first = stage_temp(&store, "upload-a", bytes).await;
        store.commit_temp(&first, &digest).await.expect("commit 1");
        let second = stage_temp(&store, "upload-b", bytes).await;
        store.commit_temp(&second, &digest).await.expect("commit 2");

        // The duplicate temp is cleaned up and the original blob is untouched.
        assert!(!tokio::fs::try_exists(&second).await.unwrap());
        let stored = tokio::fs::read(store.blob_path(&digest)).await.unwrap();
        assert_eq!(stored, bytes);
    }

    #[tokio::test]
    async fn interrupted_commit_never_appears_under_the_final_name() {
        let (_root, store) = fresh_store();
        let bytes = b"crashed mid-upload";
        let digest = digest_of(bytes);

        // Simulate a crash between "temp fully written" and "rename": the temp
        // exists, but no reader may ever see it at the content address.
        let temp = stage_temp(&store, "upload-crashed", bytes).await;

        assert!(!store.contains(&digest).await);
        assert!(matches!(
            store.open_blob(&digest).await,
            Err(AppError::NoSuchKey)
        ));
        assert!(tokio::fs::try_exists(&temp).await.unwrap());
    }

    #[tokio::test]
    async fn open_blob_round_trips_committed_bytes() {
        let (_root, store) = fresh_store();
        let bytes = b"read me back";
        let digest = digest_of(bytes);
        let temp = stage_temp(&store, "upload-rt", bytes).await;
        store.commit_temp(&temp, &digest).await.expect("commit");

        let mut file = store.open_blob(&digest).await.expect("open blob");
        let mut read_back = Vec::new();
        file.read_to_end(&mut read_back).await.unwrap();
        assert_eq!(read_back, bytes);
    }

    #[tokio::test]
    async fn scrub_quarantines_flipped_byte_and_blocks_open() {
        let (_root, store) = fresh_store();
        let bytes = b"integrity matters";
        let digest = digest_of(bytes);
        let temp = stage_temp(&store, "upload-scrub", bytes).await;
        store.commit_temp(&temp, &digest).await.expect("commit");

        // Deliberately corrupt the committed blob (bit flip on disk).
        let path = store.blob_path(&digest);
        let mut corrupt = bytes.to_vec();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&path, &corrupt).await.unwrap();

        let examined = store.scrub_once().await.expect("scrub");
        assert_eq!(examined, 1);
        assert!(store.scrubber.is_quarantined(&digest));
        assert!(
            matches!(store.open_blob(&digest).await, Err(AppError::Other(_))),
            "GET must not serve quarantined bytes"
        );
        assert!(
            !tokio::fs::try_exists(&path).await.unwrap(),
            "corrupt blob should leave objects/"
        );
    }

    #[tokio::test]
    async fn scrub_once_on_empty_store_examines_nothing() {
        let (_root, store) = fresh_store();
        let examined = store.scrub_once().await.expect("scrub");
        assert_eq!(examined, 0);
    }

    #[tokio::test]
    async fn scrub_verifies_intact_blobs_and_leaves_them_openable() {
        let (_root, store) = fresh_store();
        let a = commit_bytes(&store, b"alpha").await;
        let b = commit_bytes(&store, b"bravo").await;

        let examined = store.scrub_once().await.expect("scrub");
        assert_eq!(examined, 2);
        assert!(!store.scrubber.is_quarantined(&a));
        assert!(!store.scrubber.is_quarantined(&b));

        let mut file = store.open_blob(&a).await.expect("open a");
        let mut read_back = Vec::new();
        file.read_to_end(&mut read_back).await.unwrap();
        assert_eq!(read_back, b"alpha");

        let mut file = store.open_blob(&b).await.expect("open b");
        read_back.clear();
        file.read_to_end(&mut read_back).await.unwrap();
        assert_eq!(read_back, b"bravo");
    }

    #[tokio::test]
    async fn scrub_quarantines_only_the_corrupt_blob_among_neighbors() {
        let (_root, store) = fresh_store();
        let good = commit_bytes(&store, b"good neighbor").await;
        let bad = commit_bytes(&store, b"bad neighbor").await;

        let bad_path = store.blob_path(&bad);
        let mut corrupt = tokio::fs::read(&bad_path).await.unwrap();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&bad_path, &corrupt).await.unwrap();

        let examined = store.scrub_once().await.expect("scrub");
        assert_eq!(examined, 2);
        assert!(!store.scrubber.is_quarantined(&good));
        assert!(store.scrubber.is_quarantined(&bad));

        let mut file = store.open_blob(&good).await.expect("good still openable");
        let mut read_back = Vec::new();
        file.read_to_end(&mut read_back).await.unwrap();
        assert_eq!(read_back, b"good neighbor");

        assert!(matches!(
            store.open_blob(&bad).await,
            Err(AppError::Other(_))
        ));
        assert!(!tokio::fs::try_exists(&bad_path).await.unwrap());
    }

    #[tokio::test]
    async fn scrub_moves_corrupt_blob_into_quarantine_dir() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"move me aside").await;
        let path = store.blob_path(&digest);
        let mut corrupt = tokio::fs::read(&path).await.unwrap();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&path, &corrupt).await.unwrap();

        store.scrub_once().await.expect("scrub");

        let quarantined = store.scrubber.quarantine_dir.join(digest.as_str());
        assert!(
            tokio::fs::try_exists(&quarantined).await.unwrap(),
            "corrupt blob should land in quarantine/"
        );
        assert!(
            !tokio::fs::try_exists(&path).await.unwrap(),
            "corrupt blob should leave objects/"
        );
    }

    #[tokio::test]
    async fn scrub_skips_non_cas_junk_under_objects() {
        let (_root, store) = fresh_store();
        let junk = store.objects_root().join("not-a-digest.bin");
        tokio::fs::write(&junk, b"garbage").await.unwrap();

        let examined = store.scrub_once().await.expect("scrub");
        assert_eq!(examined, 0);
        assert!(tokio::fs::try_exists(&junk).await.unwrap());
    }

    #[tokio::test]
    async fn scrub_second_pass_skips_already_quarantined() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"quarantine once").await;
        let path = store.blob_path(&digest);
        let mut corrupt = tokio::fs::read(&path).await.unwrap();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&path, &corrupt).await.unwrap();

        assert_eq!(store.scrub_once().await.expect("first scrub"), 1);
        assert!(store.scrubber.is_quarantined(&digest));

        let examined = store.scrub_once().await.expect("second scrub");
        assert_eq!(examined, 0, "quarantined digests must not be re-examined");
    }

    #[tokio::test]
    async fn open_blob_range_refuses_quarantined_digest() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"range gate").await;
        let path = store.blob_path(&digest);
        let mut corrupt = tokio::fs::read(&path).await.unwrap();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&path, &corrupt).await.unwrap();

        store.scrub_once().await.expect("scrub");
        assert!(matches!(
            store.open_blob_range(&digest, 0, 0).await,
            Err(AppError::Other(_))
        ));
    }

    #[tokio::test]
    async fn commit_wakes_idle_scrubber_to_verify_new_blob() {
        let (_root, store) = fresh_store();
        let handle = store.clone().spawn_scrubber(Duration::from_secs(3600));

        // Let the background task park on empty objects/.
        tokio::task::yield_now().await;
        tokio::time::sleep(Duration::from_millis(20)).await;

        let digest = commit_bytes(&store, b"wake the auditor").await;
        let path = store.blob_path(&digest);

        // Allow the post-commit scrub pass to finish, then corrupt on disk.
        tokio::time::sleep(Duration::from_millis(50)).await;
        let mut corrupt = tokio::fs::read(&path).await.unwrap();
        corrupt[0] ^= 0xff;
        tokio::fs::write(&path, &corrupt).await.unwrap();

        // Scrubber is waiting on select(notify, long sleep); nudge Notify so
        // the next pass runs without waiting an hour.
        store.scrubber.notify.notify_one();

        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if store.scrubber.is_quarantined(&digest) {
                    break;
                }
                tokio::task::yield_now().await;
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("background scrubber should quarantine after notify");

        handle.abort();
        let _ = handle.await;
    }

    #[tokio::test]
    async fn open_blob_is_no_such_key_for_missing_digest() {
        let (_root, store) = fresh_store();
        assert!(matches!(
            store.open_blob(&digest_of(b"missing")).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn open_blob_range_reads_an_inclusive_middle_slice() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"0123456789").await;

        // [2, 5] inclusive → 4 bytes: "2345". The `end` byte is part of the range.
        let slice = store.open_blob_range(&digest, 2, 5).await.expect("range");
        assert_eq!(read_all(slice).await, b"2345");
    }

    #[tokio::test]
    async fn open_blob_range_covering_the_whole_file_equals_the_blob() {
        let (_root, store) = fresh_store();
        let bytes = b"read me back";
        let digest = commit_bytes(&store, bytes).await;

        // [0, len-1] is the entire object; must match a plain `open_blob` read.
        let slice = store
            .open_blob_range(&digest, 0, bytes.len() as u64 - 1)
            .await
            .expect("range");
        assert_eq!(read_all(slice).await, bytes);
    }

    #[tokio::test]
    async fn open_blob_range_reads_a_single_byte_when_start_equals_end() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"0123456789").await;

        // start == end is a valid one-byte range, not an empty one.
        let slice = store.open_blob_range(&digest, 4, 4).await.expect("range");
        assert_eq!(read_all(slice).await, b"4");
    }

    #[tokio::test]
    async fn open_blob_range_reads_the_final_byte() {
        let (_root, store) = fresh_store();
        let bytes = b"0123456789";
        let digest = commit_bytes(&store, bytes).await;
        let last = bytes.len() as u64 - 1;

        // The very last valid offset (len-1) is in range; len itself is not.
        let slice = store
            .open_blob_range(&digest, last, last)
            .await
            .expect("range");
        assert_eq!(read_all(slice).await, b"9");
    }

    #[tokio::test]
    async fn open_blob_range_rejects_start_after_end() {
        let (_root, store) = fresh_store();
        let digest = commit_bytes(&store, b"0123456789").await;

        assert!(matches!(
            store.open_blob_range(&digest, 5, 2).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn open_blob_range_rejects_end_at_or_past_eof() {
        let (_root, store) = fresh_store();
        let bytes = b"0123456789";
        let digest = commit_bytes(&store, bytes).await;
        let len = bytes.len() as u64;

        // end == len is one past the last valid byte (offsets are 0..len-1)…
        assert!(matches!(
            store.open_blob_range(&digest, 0, len).await,
            Err(AppError::InvalidRequest(_))
        ));
        // …and anything beyond that is out of bounds too.
        assert!(matches!(
            store.open_blob_range(&digest, 0, len + 100).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn open_blob_range_is_no_such_key_for_missing_digest() {
        let (_root, store) = fresh_store();
        assert!(matches!(
            store.open_blob_range(&digest_of(b"missing"), 0, 3).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn remove_deletes_a_committed_blob() {
        let (_root, store) = fresh_store();
        let bytes = b"garbage collected";
        let digest = digest_of(bytes);
        let temp = stage_temp(&store, "upload-gc", bytes).await;
        store.commit_temp(&temp, &digest).await.expect("commit");
        assert!(store.contains(&digest).await);

        store.remove(&digest).await.expect("remove");

        assert!(!store.contains(&digest).await);
        assert!(matches!(
            store.open_blob(&digest).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn remove_tolerates_an_already_gone_blob() {
        let (_root, store) = fresh_store();
        let digest = digest_of(b"never existed");

        // Removing a blob that was never stored is a no-op, not an error — the
        // GC may race another deleter or retry after a partial failure.
        store.remove(&digest).await.expect("remove missing");
    }

    #[tokio::test]
    async fn remove_is_idempotent() {
        let (_root, store) = fresh_store();
        let bytes = b"delete me twice";
        let digest = digest_of(bytes);
        let temp = stage_temp(&store, "upload-idem", bytes).await;
        store.commit_temp(&temp, &digest).await.expect("commit");

        store.remove(&digest).await.expect("remove 1");
        store.remove(&digest).await.expect("remove 2");
        assert!(!store.contains(&digest).await);
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(40))]

        /// For any bytes: stage → commit_temp makes the blob visible under its
        /// digest, reads back byte-for-byte, and consumes the temp.
        #[test]
        fn prop_commit_temp_round_trips_any_bytes(
            bytes in prop::collection::vec(any::<u8>(), 0..256),
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = digest_of(&bytes);
                let temp = stage_temp(&store, "prop-commit", &bytes).await;

                store.commit_temp(&temp, &digest).await.map_err(fail)?;

                prop_assert!(store.contains(&digest).await, "blob must be visible after commit");
                let mut file = store.open_blob(&digest).await.map_err(fail)?;
                let mut read_back = Vec::new();
                file.read_to_end(&mut read_back).await.map_err(fail)?;
                prop_assert_eq!(read_back, bytes, "blob must read back byte-for-byte");
                prop_assert!(
                    !tokio::fs::try_exists(&temp).await.map_err(fail)?,
                    "commit must consume the temp file"
                );
                Ok::<(), TestCaseError>(())
            })?;
        }

        /// Identical bytes committed twice (different temps) share one on-disk
        /// blob; the duplicate temp is cleaned up.
        #[test]
        fn prop_identical_bytes_dedup_via_commit_temp(
            bytes in prop::collection::vec(any::<u8>(), 0..256),
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = digest_of(&bytes);

                let first = stage_temp(&store, "prop-dedup-a", &bytes).await;
                store.commit_temp(&first, &digest).await.map_err(fail)?;
                let second = stage_temp(&store, "prop-dedup-b", &bytes).await;
                store.commit_temp(&second, &digest).await.map_err(fail)?;

                prop_assert!(
                    !tokio::fs::try_exists(&second).await.map_err(fail)?,
                    "duplicate temp must be removed on dedup"
                );
                prop_assert!(
                    tokio::fs::try_exists(store.blob_path(&digest))
                        .await
                        .map_err(fail)?,
                    "exactly one blob must remain at the content address"
                );
                let stored = tokio::fs::read(store.blob_path(&digest))
                    .await
                    .map_err(fail)?;
                prop_assert_eq!(stored, bytes, "original blob bytes must be untouched");
                Ok::<(), TestCaseError>(())
            })?;
        }

        /// A staged-but-uncommitted temp must never appear under the final name
        /// (crash between write and rename).
        #[test]
        fn prop_uncommitted_temp_never_visible(
            bytes in prop::collection::vec(any::<u8>(), 0..256),
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = digest_of(&bytes);
                let temp = stage_temp(&store, "prop-crash", &bytes).await;

                prop_assert!(!store.contains(&digest).await, "uncommitted temp must not be visible");
                prop_assert!(
                    matches!(store.open_blob(&digest).await, Err(AppError::NoSuchKey)),
                    "open_blob must be NoSuchKey before rename"
                );
                prop_assert!(
                    tokio::fs::try_exists(&temp).await.map_err(fail)?,
                    "the staged temp itself must still exist"
                );
                Ok::<(), TestCaseError>(())
            })?;
        }

        /// Inclusive range [start, end] returns exactly bytes[start..=end].
        #[test]
        fn prop_open_blob_range_matches_inclusive_slice(
            bytes in prop::collection::vec(any::<u8>(), 1..256),
            start_idx in any::<prop::sample::Index>(),
            end_idx in any::<prop::sample::Index>(),
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = commit_bytes(&store, &bytes).await;
                let len = bytes.len();
                let mut start = start_idx.index(len);
                let mut end = end_idx.index(len);
                if start > end {
                    std::mem::swap(&mut start, &mut end);
                }

                let slice = store
                    .open_blob_range(&digest, start as u64, end as u64)
                    .await
                    .map_err(fail)?;
                prop_assert_eq!(
                    read_all(slice).await,
                    bytes[start..=end].to_vec(),
                    "range must equal the inclusive slice"
                );
                Ok::<(), TestCaseError>(())
            })?;
        }

        /// Invalid bounds (start > end, or end past EOF) are always InvalidRequest.
        #[test]
        fn prop_open_blob_range_rejects_invalid_bounds(
            bytes in prop::collection::vec(any::<u8>(), 1..256),
            start in 0u64..512,
            end in 0u64..512,
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = commit_bytes(&store, &bytes).await;
                let len = bytes.len() as u64;

                // Only assert when the pair is actually invalid for this blob.
                prop_assume!(start > end || end >= len);

                prop_assert!(
                    matches!(
                        store.open_blob_range(&digest, start, end).await,
                        Err(AppError::InvalidRequest(_))
                    ),
                    "invalid bounds must be InvalidRequest (start={start}, end={end}, len={len})"
                );
                Ok::<(), TestCaseError>(())
            })?;
        }

        /// remove is idempotent: twice after commit, and once for a never-stored digest.
        #[test]
        fn prop_remove_is_idempotent_for_any_blob(
            bytes in prop::collection::vec(any::<u8>(), 0..256),
        ) {
            block_on(async {
                let (_root, store) = fresh_store();
                let digest = digest_of(&bytes);
                let temp = stage_temp(&store, "prop-remove", &bytes).await;
                store.commit_temp(&temp, &digest).await.map_err(fail)?;

                store.remove(&digest).await.map_err(fail)?;
                store.remove(&digest).await.map_err(fail)?;

                prop_assert!(!store.contains(&digest).await, "blob must be gone after remove");
                prop_assert!(
                    matches!(store.open_blob(&digest).await, Err(AppError::NoSuchKey)),
                    "open_blob must be NoSuchKey after remove"
                );

                let never = digest_of(b"prop-never-stored-sentinel");
                store.remove(&never).await.map_err(fail)?;
                Ok::<(), TestCaseError>(())
            })?;
        }
    }
}
