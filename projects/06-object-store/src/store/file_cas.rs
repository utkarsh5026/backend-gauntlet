//! One-file-per-digest CAS layout — today's default physical store.
//!
//! Each distinct blob lives at `objects/<ab>/<cd>/<sha256>` and is published
//! with the shared temp→fsync→rename dance in [`crate::durable`]. This is the
//! layout [`super::Store`] used before packing was scaffolded; it remains
//! the default write policy ([`super::BlobLayoutKind::FileCas`]).
//!
//! See [`super::haystack`] for the append-only volume alternative and
//! [`docs/11-how-haystack-packing-works.md`](../../docs/11-how-haystack-packing-works.md).

use std::path::{Path, PathBuf};

use crate::durable;
use crate::error::AppError;
use crate::object::Digest;

/// Content-addressed tree: one committed file per digest under `objects/`.
#[derive(Debug)]
pub struct FileCas {
    objects: PathBuf,
}

impl FileCas {
    /// Create `objects/` under `root` if needed and return the layout handle.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the directory cannot be created.
    pub fn open(root: impl AsRef<Path>) -> std::io::Result<Self> {
        let objects = root.as_ref().join("objects");
        std::fs::create_dir_all(&objects)?;
        Ok(Self { objects })
    }

    /// Root of the committed blob tree (`…/objects`).
    pub fn objects_root(&self) -> &Path {
        &self.objects
    }

    /// Map a digest to its sharded on-disk blob path (`objects/ab/cd/<64-hex>`).
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

    /// Inverse of [`Self::blob_path`]: recover a digest from a sharded blob path.
    ///
    /// Accepts only paths under [`Self::objects_root`] with the exact layout
    /// `ab/cd/<64-hex>` (two 2-char hex shards, then a 64-char ASCII hex name).
    /// Returns `None` for paths outside the tree, wrong depth, non-UTF-8
    /// components, or a name that is not 64 hex digits.
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

    /// Depth-first walk of [`Self::objects_root`], counting committed blob files.
    ///
    /// Returns `(blob_count, total_bytes)` where `blob_count` is the number of
    /// regular files under the tree and `total_bytes` is the sum of their
    /// lengths. Directories and non-file entries are skipped; no digest
    /// validation is performed — any file in the tree is counted.
    ///
    /// # Errors
    ///
    /// Propagates I/O errors from directory reads or metadata lookups.
    pub fn scan_occupancy(&self) -> std::io::Result<(u64, u64)> {
        let mut blob_count = 0u64;
        let mut total_bytes = 0u64;
        let mut stack = vec![self.objects.clone()];
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

    /// Whether a committed blob file exists for `digest` under this layout.
    ///
    /// Uses [`Self::blob_path`]; I/O errors from the existence check are treated
    /// as absent (`false`).
    pub async fn contains(&self, digest: &Digest) -> bool {
        tokio::fs::try_exists(self.blob_path(digest))
            .await
            .unwrap_or(false)
    }

    /// Publish a fully written temp file at the content-addressed path.
    ///
    /// Caller is responsible for the dedup-hit short-circuit (delete temp when
    /// [`Self::contains`] is already true) and for metrics / scrubber wake.
    ///
    /// # Panics
    ///
    /// Panics if [`Self::blob_path`] has no parent.
    pub async fn commit_temp(&self, temp: &Path, digest: &Digest) -> Result<(), AppError> {
        let blob_path = self.blob_path(digest);
        durable::publish_temp(temp, &blob_path).await
    }

    pub fn list_digests(&self) -> std::io::Result<Vec<Digest>> {
        let mut digests = Vec::new();
        let mut stack = vec![self.objects.clone()];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(dir)? {
                let entry = entry?;
                let metadata = entry.metadata()?;
                if metadata.is_dir() {
                    stack.push(entry.path());
                } else if metadata.is_file() {
                    if let Some(digest) = self.digest_from_path(&entry.path()) {
                        digests.push(digest);
                    }
                }
            }
        }
        Ok(digests)
    }

    /// Open a committed blob file (no quarantine check — [`super::Store`] owns that).
    pub async fn open_blob(&self, digest: &Digest) -> Result<tokio::fs::File, AppError> {
        let path = self.blob_path(digest);
        if !tokio::fs::try_exists(&path).await? {
            return Err(AppError::NoSuchKey);
        }
        Ok(tokio::fs::File::open(path).await?)
    }

    /// Remove a committed blob if present. Idempotent.
    ///
    /// Returns the removed size when a file was deleted, or `None` if absent.
    pub async fn remove(&self, digest: &Digest) -> Result<Option<u64>, AppError> {
        let path = self.blob_path(digest);
        if !tokio::fs::try_exists(&path).await? {
            return Ok(None);
        }
        let size = tokio::fs::metadata(&path).await?.len();
        tokio::fs::remove_file(path).await?;
        Ok(Some(size))
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

    fn fresh() -> (TempDir, FileCas) {
        let root = TempDir::new().unwrap();
        let cas = FileCas::open(root.path()).unwrap();
        (root, cas)
    }

    async fn stage(root: &Path, name: &str, bytes: &[u8]) -> PathBuf {
        let temp = root.join(name);
        tokio::fs::write(&temp, bytes).await.unwrap();
        temp
    }

    async fn read_blob(cas: &FileCas, digest: &Digest) -> Vec<u8> {
        let mut file = cas.open_blob(digest).await.expect("open blob");
        let mut got = Vec::new();
        file.read_to_end(&mut got).await.unwrap();
        got
    }

    #[test]
    fn open_creates_objects_directory() {
        let root = TempDir::new().unwrap();
        let cas = FileCas::open(root.path()).unwrap();
        assert!(cas.objects_root().is_dir());
        assert_eq!(cas.objects_root(), root.path().join("objects"));
    }

    #[test]
    fn blob_path_fans_out_deterministically() {
        let (root, cas) = fresh();
        let digest = digest_of(b"hello");
        let hex = digest.as_str();
        let path = cas.blob_path(&digest);

        assert_eq!(
            path,
            root.path()
                .join("objects")
                .join(&hex[0..2])
                .join(&hex[2..4])
                .join(hex),
        );
        assert_eq!(path, cas.blob_path(&digest));
        assert_eq!(path.file_name().unwrap().to_str().unwrap(), hex);
    }

    #[test]
    fn digest_from_path_inverts_blob_path() {
        let (_root, cas) = fresh();
        let digest = digest_of(b"round-trip");
        let path = cas.blob_path(&digest);
        assert_eq!(cas.digest_from_path(&path), Some(digest));
    }

    #[test]
    fn digest_from_path_rejects_paths_outside_objects() {
        let (root, cas) = fresh();
        let digest = digest_of(b"outside");
        // Same shard layout, but rooted under the store root instead of objects/.
        let outsider = root
            .path()
            .join(&digest.as_str()[0..2])
            .join(&digest.as_str()[2..4])
            .join(digest.as_str());
        assert_eq!(cas.digest_from_path(&outsider), None);
    }

    #[test]
    fn digest_from_path_rejects_wrong_depth() {
        let (_root, cas) = fresh();
        let digest = digest_of(b"depth");
        let hex = digest.as_str();
        let objects = cas.objects_root();

        assert_eq!(cas.digest_from_path(objects), None);
        assert_eq!(cas.digest_from_path(&objects.join(&hex[0..2])), None);
        assert_eq!(
            cas.digest_from_path(&objects.join(&hex[0..2]).join(&hex[2..4])),
            None
        );
        assert_eq!(
            cas.digest_from_path(
                &objects
                    .join(&hex[0..2])
                    .join(&hex[2..4])
                    .join(hex)
                    .join("extra")
            ),
            None
        );
    }

    #[test]
    fn digest_from_path_rejects_bad_shard_or_name_shape() {
        let (_root, cas) = fresh();
        let objects = cas.objects_root();
        let good = "a".repeat(64);

        // Shard components must be exactly two characters each.
        assert_eq!(
            cas.digest_from_path(&objects.join("a").join("bc").join(good.as_str())),
            None
        );
        assert_eq!(
            cas.digest_from_path(&objects.join("ab").join("c").join(good.as_str())),
            None
        );
        // Filename must be exactly 64 hex digits.
        assert_eq!(
            cas.digest_from_path(&objects.join("ab").join("cd").join("abc")),
            None
        );
        assert_eq!(
            cas.digest_from_path(&objects.join("ab").join("cd").join("g".repeat(64))),
            None
        );
        assert_eq!(
            cas.digest_from_path(&objects.join("ab").join("cd").join(format!("{good}ff"))),
            None
        );
    }

    #[test]
    fn scan_occupancy_is_empty_on_a_fresh_tree() {
        let (_root, cas) = fresh();
        assert_eq!(cas.scan_occupancy().unwrap(), (0, 0));
    }

    #[tokio::test]
    async fn scan_occupancy_counts_committed_blob_files() {
        let (root, cas) = fresh();
        let a = b"alpha-bytes";
        let b = b"beta-bytes!!";
        let da = digest_of(a);
        let db = digest_of(b);

        cas.commit_temp(&stage(root.path(), "a", a).await, &da)
            .await
            .unwrap();
        cas.commit_temp(&stage(root.path(), "b", b).await, &db)
            .await
            .unwrap();

        let (count, bytes) = cas.scan_occupancy().unwrap();
        assert_eq!(count, 2);
        assert_eq!(bytes, (a.len() + b.len()) as u64);
    }

    #[tokio::test]
    async fn contains_is_false_for_unknown_digest() {
        let (_root, cas) = fresh();
        assert!(!cas.contains(&digest_of(b"never stored")).await);
    }

    #[tokio::test]
    async fn commit_makes_blob_visible_and_consumes_the_temp() {
        let (root, cas) = fresh();
        let bytes = b"file-cas-bytes";
        let digest = digest_of(bytes);
        let temp = stage(root.path(), "tmp-blob", bytes).await;

        cas.commit_temp(&temp, &digest).await.unwrap();

        assert!(cas.contains(&digest).await);
        assert_eq!(read_blob(&cas, &digest).await, bytes);
        assert_eq!(
            tokio::fs::read(cas.blob_path(&digest)).await.unwrap(),
            bytes
        );
        assert!(!tokio::fs::try_exists(&temp).await.unwrap());
    }

    #[tokio::test]
    async fn open_blob_is_no_such_key_for_missing_digest() {
        let (_root, cas) = fresh();
        assert!(matches!(
            cas.open_blob(&digest_of(b"missing")).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn remove_deletes_a_committed_blob_and_returns_its_size() {
        let (root, cas) = fresh();
        let bytes = b"garbage collected";
        let digest = digest_of(bytes);
        cas.commit_temp(&stage(root.path(), "gc", bytes).await, &digest)
            .await
            .unwrap();

        let removed = cas.remove(&digest).await.unwrap();
        assert_eq!(removed, Some(bytes.len() as u64));
        assert!(!cas.contains(&digest).await);
        assert!(matches!(
            cas.open_blob(&digest).await,
            Err(AppError::NoSuchKey)
        ));
    }

    #[tokio::test]
    async fn remove_is_idempotent_for_missing_blobs() {
        let (root, cas) = fresh();
        let bytes = b"delete me twice";
        let digest = digest_of(bytes);
        cas.commit_temp(&stage(root.path(), "idem", bytes).await, &digest)
            .await
            .unwrap();

        assert_eq!(cas.remove(&digest).await.unwrap(), Some(bytes.len() as u64));
        assert_eq!(cas.remove(&digest).await.unwrap(), None);
        assert_eq!(
            cas.remove(&digest_of(b"never existed")).await.unwrap(),
            None
        );
    }
}
