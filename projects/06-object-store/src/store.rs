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

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::AppError;
use crate::object::Digest;

/// RAII guard over a staged temp file: deletes it on drop unless [`disarm`]ed.
///
/// The pattern is "stage → hash/write → atomically publish". A writer creates
/// the guard, streams bytes into [`path`](Self::path), and leaves cleanup to
/// `Drop`: any early `?`-return (oversize, I/O error, validation failure) or a
/// dropped future unlinks the half-written temp automatically — so no error arm
/// has to remember to delete it. Once the bytes are durably published (renamed
/// into the blob tree by [`commit_temp`](Store::commit_temp), or promoted into a
/// staging area) the writer [`disarm`](Self::disarm)s the guard so the
/// now-durable file survives.
///
/// [`disarm`]: Self::disarm
pub struct TempEntry(Option<PathBuf>);

impl TempEntry {
    /// Wrap `path` in a guard. The file it names is removed on drop until
    /// [`disarm`](Self::disarm) is called — use this to guard temps outside the
    /// store's own `tmp/` (e.g. a multipart part file in its staging area).
    pub fn new(path: PathBuf) -> Self {
        Self(Some(path))
    }

    /// The staged path — write your in-flight bytes here.
    pub fn path(&self) -> &Path {
        self.0.as_ref().expect("temp path is not None")
    }

    /// Give up ownership of the temp path so [`Drop`] won't delete it.
    ///
    /// Called once the file has been durably published (renamed into its
    /// committed location, or promoted into a staging area) — it is no longer
    /// garbage, so the guard must not reap it.
    pub fn disarm(&mut self) {
        self.0 = None;
    }
}

impl Drop for TempEntry {
    fn drop(&mut self) {
        if let Some(path) = self.0.take() {
            let _ = std::fs::remove_file(&path);
        }
    }
}

/// The on-disk blob store. Owns the `objects/` tree (committed, content-named
/// blobs) and the `tmp/` tree (in-flight writes, before they're atomically
/// renamed into place).
pub struct Store {
    objects: PathBuf,
    tmp: PathBuf,
}

impl Store {
    /// Open (creating if needed) the blob store under `root`. Plumbing — the
    /// interesting methods below are yours to build.
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
        Ok(Arc::new(Self { objects, tmp }))
    }

    /// Where V2 stages an in-flight write before committing it. A temp file here
    /// is renamed onto its final `blob_path` only once fully written + fsync'd.
    pub fn tmp_dir(&self) -> &Path {
        &self.tmp
    }

    /// A unique path under [`Self::tmp_dir`] for staging an in-flight blob write.
    ///
    /// `prefix` labels the caller (e.g. `"stream"`, `"multipart"`); uniqueness
    /// comes from the trailing epoch nanos hex.
    pub fn tmp_file(&self, prefix: &str) -> TempEntry {
        let id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time before UNIX epoch")
            .as_nanos();

        let path = self.tmp.join(format!("{prefix}-{id:x}"));
        TempEntry::new(path)
    }

    /// Map a digest to its on-disk path, fanned out by the leading hash bytes
    /// (`objects/ab/cd/abcd…`) so no single directory holds millions of entries.
    pub fn blob_path(&self, digest: &Digest) -> PathBuf {
        self.objects
            .join(&digest.as_str()[0..2])
            .join(&digest.as_str()[2..4])
            .join(digest.as_str())
    }

    /// Inverse of [`Self::blob_path`]: recover the digest from a path under
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

    /// Does a blob with this digest already exist? This is the dedup check —
    /// if it does, identical bytes are already stored and we skip the write.
    pub async fn contains(&self, digest: &Digest) -> bool {
        tokio::fs::try_exists(self.blob_path(digest))
            .await
            .unwrap_or(false)
    }

    /// Commit a fully-written *temp* file as the blob for `digest`, durably and
    /// atomically. This is the heart of V1.
    pub async fn commit_temp(&self, temp: &Path, digest: &Digest) -> Result<(), AppError> {
        use tokio::fs as tfs;
        if self.contains(digest).await {
            tfs::remove_file(temp).await?;
            return Ok(());
        }
        tfs::File::open(temp).await?.sync_all().await?;
        let blob_path = self.blob_path(digest);
        let parent = blob_path
            .parent()
            .expect("blob path has a parent directory");

        tfs::create_dir_all(parent).await?;
        tfs::rename(temp, &blob_path).await?;
        tfs::File::open(parent).await?.sync_all().await?;
        Ok(())
    }

    pub async fn open_blob(&self, digest: &Digest) -> Result<tokio::fs::File, AppError> {
        use tokio::fs as tfs;

        let path = self.blob_path(digest);
        if !tfs::try_exists(&path).await? {
            return Err(AppError::NoSuchKey);
        }

        Ok(tfs::File::open(path).await?)
    }

    /// Root of the committed blob tree (`objects/`). Used by V3 GC sweep.
    pub fn objects_root(&self) -> &Path {
        &self.objects
    }

    pub async fn remove(&self, digest: &Digest) -> Result<(), AppError> {
        use tokio::fs as tfs;
        let path = self.blob_path(digest);
        if !tfs::try_exists(&path).await? {
            return Ok(());
        }
        tfs::remove_file(&path).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest as _, Sha256};
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

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
    async fn open_blob_is_no_such_key_for_missing_digest() {
        let (_root, store) = fresh_store();
        assert!(matches!(
            store.open_blob(&digest_of(b"missing")).await,
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
}
