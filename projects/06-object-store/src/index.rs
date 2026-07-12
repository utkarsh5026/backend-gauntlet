//! V3 — The bucket/key namespace + a crash-safe index, with prefix listing & GC.
//!
//! This maps `(bucket, key) → blob` and owns the rules that keep that mapping
//! consistent with V1's blobs across crashes and deletes. Two ideas to hold:
//!   - the keyspace is **flat** (`a/b/c.jpg` is one opaque key) — `ListObjectsV2`
//!     only *pretends* it's a tree via prefix/delimiter;
//!   - the write order is a contract: **blob durable (V2) → THEN index entry**,
//!     so a crash in between leaves a GC-able orphan blob, never a dangling key.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use futures_util::stream::{self, StreamExt, TryStreamExt};
use tokio::io::AsyncWriteExt;

use crate::error::AppError;
use crate::naming::{encode_key, validate_bucket_name, validate_key};
use crate::object::{Digest, ObjectMeta};
use crate::store::{Store, TempEntry};

/// Maps `(bucket, key)` → the blob (and metadata) backing it, and owns the
/// consistency + reclamation rules over the V1 store.
pub struct Index {
    root: PathBuf,
    store: Arc<Store>,
}

/// One page of a `ListObjectsV2` response.
pub struct Listing {
    /// The objects on this page (under the prefix, not rolled into a sub-prefix).
    pub objects: Vec<ObjectMeta>,
    /// Keys rolled up under the delimiter — the faked "subdirectories". With
    /// `delimiter=/` and `prefix=a/`, keys `a/b/c` and `a/b/d` both collapse to
    /// the single common prefix `a/b/`.
    pub common_prefixes: Vec<String>,
    /// Set when the listing was truncated at `max_keys`; pass it back to resume.
    pub next_continuation_token: Option<String>,
}

/// One row in a merged objects + common-prefixes listing page.
enum ListItem {
    Object(ObjectMeta),
    Prefix(String),
}

impl ListItem {
    fn sort_key(&self) -> &str {
        match self {
            ListItem::Object(meta) => &meta.key,
            ListItem::Prefix(prefix) => prefix,
        }
    }
}

impl Index {
    const OBJECTS_DIR: &str = "objects";
    const TMP_DIR: &str = "tmp";
    /// Parallel JSON reads during GC mark — bounded so we don't thrash the disk.
    const GC_READ_CONCURRENCY: usize = 32;

    /// How long a blob must sit unreferenced before GC may reclaim it.
    ///
    /// The grace window protects blobs that are durable but whose committed
    /// index entry hasn't been renamed into place yet (the dedup-in-flight
    /// window). Zero under `cfg!(test)` so tests can assert reclamation without
    /// sleeping; the tmp-scan in [`gc`](Self::gc) is what protects in-flight
    /// PUTs when grace is zero.
    fn gc_grace() -> Duration {
        if cfg!(test) {
            Duration::ZERO
        } else {
            Duration::from_secs(60)
        }
    }

    /// Open (creating if needed) the index under `root/index`. The index needs a
    /// handle to the store so its GC can remove now-unreferenced blobs.
    pub fn open(root: impl AsRef<Path>, store: Arc<Store>) -> std::io::Result<Arc<Self>> {
        use std::fs::create_dir_all;

        let root = root.as_ref().join("index");
        create_dir_all(&root)?;
        Ok(Arc::new(Self { root, store }))
    }

    /// Create a new, empty bucket directory under the index root.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] if `bucket` fails S3 naming rules (see
    /// [`validate_bucket_name`](crate::naming::validate_bucket_name)), or
    /// [`AppError::BucketAlreadyExists`] if the bucket is already present.
    pub async fn create_bucket(&self, bucket: &str) -> Result<(), AppError> {
        validate_bucket_name(bucket)?;
        let bucket_path = self.root.join(bucket);
        if tokio::fs::try_exists(&bucket_path).await? {
            return Err(AppError::BucketAlreadyExists);
        }
        tokio::fs::create_dir_all(&bucket_path).await?;
        Ok(())
    }

    /// Error with [`AppError::NoSuchBucket`] when no bucket with that name exists.
    ///
    /// Validates the bucket name first (see [`validate_bucket_name`]).
    pub async fn ensure_bucket(&self, bucket: &str) -> Result<(), AppError> {
        validate_bucket_name(bucket)?;
        if !tokio::fs::try_exists(self.root.join(bucket)).await? {
            return Err(AppError::NoSuchBucket);
        }
        Ok(())
    }

    pub async fn put(&self, meta: ObjectMeta) -> Result<(), AppError> {
        use tokio::fs as tfs;

        let ObjectMeta { bucket, key, .. } = &meta;
        let path = self.index_path(bucket, key)?;
        let temp_path = self.temp_entry_path(bucket, key);
        let mut temp_guard = TempEntry::new(temp_path.clone());

        tfs::create_dir_all(self.objects_dir(bucket)).await?;
        tfs::create_dir_all(self.tmp_dir(bucket)).await?;

        let bytes = serde_json::to_vec(&meta)?;
        let mut file = tfs::File::create(&temp_path).await?;
        file.write_all(&bytes).await?;
        file.sync_all().await?;
        tfs::rename(&temp_path, &path).await?;
        temp_guard.disarm();
        metrics::counter!(crate::metrics::OBJECTS_PUT_TOTAL).increment(1);
        metrics::histogram!(crate::metrics::OBJECT_SIZE_BYTES).record(meta.size as f64);
        Ok(())
    }

    /// Resolve `(bucket, key)` to its [`ObjectMeta`], or `None` if no such key.
    ///
    /// Reads only the JSON pointer — it never opens the underlying blob.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] for a bad bucket name, or an I/O /
    /// deserialization error if the pointer exists but can't be read as
    /// [`ObjectMeta`].
    pub async fn get(&self, bucket: &str, key: &str) -> Result<Option<ObjectMeta>, AppError> {
        let path = self.index_path(bucket, key)?;
        if !tokio::fs::try_exists(&path).await? {
            return Ok(None);
        }
        Ok(Some(read_json(&path).await?))
    }

    /// Drop the `(bucket, key)` pointer; idempotent if the key is already gone.
    ///
    /// This only removes the index entry — never the blob. Bytes shared by
    /// another key must survive; the now-unreferenced blob, if any, is left for
    /// [`gc`](Self::gc) to reclaim later.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] for a bad bucket name, or an I/O error
    /// removing the entry.
    pub async fn delete(&self, bucket: &str, key: &str) -> Result<(), AppError> {
        let path = self.index_path(bucket, key)?;
        if tokio::fs::try_exists(&path).await? {
            tokio::fs::remove_file(&path).await?;
        }
        metrics::counter!(crate::metrics::OBJECTS_DELETED_TOTAL).increment(1);
        Ok(())
    }

    /// One page of a `ListObjectsV2`-style listing over a bucket.
    ///
    /// `prefix` filters keys; `delimiter` (typically `/`) rolls keys sharing the
    /// next segment into [`Listing::common_prefixes`] — the folder illusion over
    /// the flat keyspace. `continuation` resumes after a previous page's token,
    /// and `max_keys` caps objects + common prefixes per page, setting
    /// [`Listing::next_continuation_token`] when more remain. Objects are
    /// returned sorted by key.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] for a bad bucket name, or an I/O /
    /// deserialization error reading an index entry.
    pub async fn list(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        continuation: Option<&str>,
        max_keys: usize,
    ) -> Result<Listing, AppError> {
        validate_bucket_name(bucket)?;

        let mut paths = Vec::new();
        Self::push_index_files(&mut paths, &self.objects_dir(bucket)).await?;

        let mut objects = Self::read_index_entries(paths).await?;
        objects.retain(|meta| meta.key.starts_with(prefix));

        objects.sort_by(|a, b| a.key.cmp(&b.key));

        let mut common_prefixes = Vec::new();
        if let Some(delim) = delimiter {
            let mut rolled = HashSet::new();
            let mut leaves = Vec::new();
            for meta in objects {
                let remainder = meta.key.strip_prefix(prefix).unwrap_or(&meta.key);
                if let Some(idx) = remainder.find(delim) {
                    let end = idx + delim.len();
                    rolled.insert(format!("{}{}", prefix, &remainder[..end]));
                } else {
                    leaves.push(meta);
                }
            }
            objects = leaves;
            common_prefixes = rolled.into_iter().collect();
            common_prefixes.sort();
        }

        let mut items: Vec<ListItem> = objects
            .into_iter()
            .map(ListItem::Object)
            .chain(common_prefixes.into_iter().map(ListItem::Prefix))
            .collect();
        items.sort_by(|a, b| a.sort_key().cmp(b.sort_key()));

        if let Some(token) = continuation {
            items.retain(|item| item.sort_key() > token);
        }

        let next_continuation_token = if items.len() > max_keys {
            Some(items[max_keys - 1].sort_key().to_string())
        } else {
            None
        };
        items.truncate(max_keys);

        let mut objects = Vec::new();
        let mut common_prefixes = Vec::new();
        for item in items {
            match item {
                ListItem::Object(meta) => objects.push(meta),
                ListItem::Prefix(prefix) => common_prefixes.push(prefix),
            }
        }

        Ok(Listing {
            objects,
            common_prefixes,
            next_continuation_token,
        })
    }

    /// Mark-and-sweep the blob store, reclaiming blobs no live key references.
    ///
    /// Mark: gather every digest referenced by a committed index entry *and* by
    /// an in-flight `tmp/` entry (see
    /// [`collect_referenced_digests`](Self::collect_referenced_digests)). Sweep:
    /// walk the store's object tree and remove any blob that is unreferenced
    /// *and* older than [`gc_grace`](Self::gc_grace) — the grace window plus the
    /// tmp-scan together keep a committing PUT's not-yet-renamed blob safe.
    /// Returns the number of blobs reclaimed.
    ///
    /// # Errors
    ///
    /// An I/O error walking the index or store, or from
    /// [`Store::remove`](crate::store::Store::remove). A corrupt `tmp/` entry is
    /// skipped rather than aborting the sweep.
    pub async fn gc(&self) -> Result<u64, AppError> {
        let referenced = self.collect_referenced_digests().await?;
        let grace_cutoff = SystemTime::now()
            .checked_sub(Self::gc_grace())
            .unwrap_or(UNIX_EPOCH);
        let mut reclaimed = 0u64;

        let mut stack = vec![self.store.objects_root().to_path_buf()];
        while let Some(dir) = stack.pop() {
            let mut entries = tokio::fs::read_dir(&dir).await?;
            while let Some(entry) = entries.next_entry().await? {
                let path = entry.path();
                let file_type = entry.file_type().await?;

                if file_type.is_dir() {
                    stack.push(path);
                    continue;
                }

                if !file_type.is_file() {
                    continue;
                }

                let Some(digest) = self.store.digest_from_path(&path) else {
                    continue;
                };

                if referenced.contains(&digest)
                    || tokio::fs::metadata(&path).await?.modified()? > grace_cutoff
                {
                    continue;
                }

                self.store.remove(&digest).await?;
                metrics::counter!(crate::metrics::GC_BLOBS_RECLAIMED_TOTAL).increment(1);
                reclaimed += 1;
            }
        }

        Ok(reclaimed)
    }

    /// Collect every digest the store must keep: committed pointers *and*
    /// in-flight `tmp/` entries, across all buckets.
    ///
    /// Committed reads that fail to parse propagate as errors (a corrupt
    /// committed entry is a real problem), but a malformed `tmp/` entry — the
    /// expected shape of a crash mid-`put` — is silently ignored so a single
    /// half-written temp can't wedge GC. Including tmp digests is what protects a
    /// blob whose committed pointer hasn't been renamed into place yet.
    async fn collect_referenced_digests(&self) -> Result<HashSet<Digest>, AppError> {
        let (committed, tmp_files) = {
            let mut buckets = tokio::fs::read_dir(&self.root).await?;
            let mut committed = Vec::new();
            let mut tmp_files = Vec::new();

            while let Some(bucket) = buckets.next_entry().await? {
                if !bucket.file_type().await?.is_dir() {
                    continue;
                }
                let bucket_name = bucket.file_name().to_string_lossy().into_owned();
                Self::push_index_files(&mut committed, &self.objects_dir(&bucket_name)).await?;
                Self::push_index_files(&mut tmp_files, &self.tmp_dir(&bucket_name)).await?;
            }

            (committed, tmp_files)
        };

        let committed_digests = Self::read_index_entries(committed)
            .await?
            .into_iter()
            .map(|m| m.digest)
            .collect::<Vec<_>>();

        let tmp_digests = stream::iter(tmp_files)
            .map(|path| async move { read_json(&path).await.ok().map(|m| m.digest) })
            .buffer_unordered(Self::GC_READ_CONCURRENCY)
            .filter_map(futures_util::future::ready)
            .collect::<Vec<_>>()
            .await;

        Ok(committed_digests.into_iter().chain(tmp_digests).collect())
    }

    async fn push_index_files(out: &mut Vec<PathBuf>, dir: &Path) -> Result<(), AppError> {
        if !tokio::fs::try_exists(dir).await? {
            return Ok(());
        }
        let mut index_files = tokio::fs::read_dir(dir).await?;
        while let Some(entry) = index_files.next_entry().await? {
            if entry.file_type().await?.is_file() {
                out.push(entry.path());
            }
        }
        Ok(())
    }

    fn temp_entry_path(&self, bucket: &str, key: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time before UNIX epoch")
            .as_nanos();
        let key = encode_key(key);
        self.tmp_dir(bucket).join(format!("{}-{nanos:x}.json", key))
    }

    async fn read_index_entries(paths: Vec<PathBuf>) -> Result<Vec<ObjectMeta>, AppError> {
        stream::iter(paths)
            .map(|path| async move { read_json(&path).await })
            .buffer_unordered(Self::GC_READ_CONCURRENCY)
            .try_collect()
            .await
    }

    #[inline]
    fn index_path(&self, bucket: &str, key: &str) -> Result<PathBuf, AppError> {
        validate_bucket_name(bucket)?;
        validate_key(key)?;
        Ok(self
            .objects_dir(bucket)
            .join(format!("{}.json", encode_key(key))))
    }

    #[inline]
    fn objects_dir(&self, bucket: &str) -> PathBuf {
        self.root.join(bucket).join(Self::OBJECTS_DIR)
    }

    #[inline]
    fn tmp_dir(&self, bucket: &str) -> PathBuf {
        self.root.join(bucket).join(Self::TMP_DIR)
    }
}

async fn read_json(path: &Path) -> Result<ObjectMeta, AppError> {
    let json = tokio::fs::read_to_string(path).await?;
    let meta = serde_json::from_str::<ObjectMeta>(&json)?;
    Ok(meta)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::object::{Digest, ETag};
    use crate::streaming::{stream_to_store, Stored};
    use bytes::Bytes;
    use futures_util::stream;
    use proptest::prelude::*;
    use proptest::test_runner::TestCaseError;
    use tempfile::TempDir;

    /// An Index rooted in a throwaway temp dir, over a real store.
    fn fresh() -> (TempDir, Arc<Index>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        let index = Index::open(root.path(), store).expect("open index");
        (root, index)
    }

    /// Like `fresh`, but also hands back the `Store` so consistency tests can
    /// assert on the blobs behind the pointers (dedup, GC reclamation).
    fn fresh_full() -> (TempDir, Arc<Store>, Arc<Index>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        let index = Index::open(root.path(), store.clone()).expect("open index");
        (root, store, index)
    }

    /// Stream real bytes through V2/V1 into the store, returning the content
    /// digest + etag + size — a genuine blob the GC can later reclaim.
    async fn store_bytes(store: &Store, bytes: &[u8]) -> Stored {
        let chunk: Result<Bytes, axum::Error> = Ok(Bytes::copy_from_slice(bytes));
        stream_to_store(store, stream::iter(vec![chunk]), 1 << 20)
            .await
            .expect("storing bytes should succeed")
    }

    /// An index entry pointing `(bucket, key)` at an already-stored blob.
    fn meta_for(bucket: &str, key: &str, stored: &Stored) -> ObjectMeta {
        ObjectMeta {
            bucket: bucket.into(),
            key: key.into(),
            digest: stored.digest.clone(),
            size: stored.size,
            etag: stored.etag.clone(),
            content_type: "image/jpeg".into(),
            last_modified: chrono::Utc::now(),
        }
    }

    #[tokio::test]
    async fn create_bucket_accepts_a_valid_name() {
        let (_root, index) = fresh();
        index
            .create_bucket("photos")
            .await
            .expect("a valid S3 bucket name must succeed");
    }

    #[tokio::test]
    async fn create_bucket_rejects_names_outside_length_bounds() {
        let (_root, index) = fresh();
        assert!(
            matches!(
                index.create_bucket("ab").await,
                Err(AppError::InvalidRequest(_))
            ),
            "2 chars is below the 3-char minimum"
        );
        let too_long = "a".repeat(64);
        assert!(
            matches!(
                index.create_bucket(&too_long).await,
                Err(AppError::InvalidRequest(_))
            ),
            "64 chars is above the 63-char maximum"
        );
    }

    /// The whitelist that also closes path traversal: anything with '/', '.',
    /// '_', or uppercase can't name a bucket, so it can't escape the data dir.
    #[tokio::test]
    async fn create_bucket_rejects_illegal_chars_and_traversal() {
        let (_root, index) = fresh();
        for bad in ["Photos", "my_bucket", "a/b", "../etc", "my.bucket"] {
            assert!(
                matches!(
                    index.create_bucket(bad).await,
                    Err(AppError::InvalidRequest(_))
                ),
                "{bad:?} must be rejected as an invalid bucket name"
            );
        }
    }

    #[tokio::test]
    async fn create_bucket_rejects_leading_or_trailing_hyphen() {
        let (_root, index) = fresh();
        assert!(
            matches!(
                index.create_bucket("-photos").await,
                Err(AppError::InvalidRequest(_))
            ),
            "a leading hyphen is not a valid S3 bucket name"
        );
        assert!(
            matches!(
                index.create_bucket("photos-").await,
                Err(AppError::InvalidRequest(_))
            ),
            "a trailing hyphen is not a valid S3 bucket name"
        );
    }

    #[tokio::test]
    async fn create_bucket_conflicts_on_duplicate() {
        let (_root, index) = fresh();
        index
            .create_bucket("photos")
            .await
            .expect("first create should succeed");
        assert!(
            matches!(
                index.create_bucket("photos").await,
                Err(AppError::BucketAlreadyExists)
            ),
            "creating an existing bucket must be a conflict, not a silent success"
        );
    }

    /// A synthetic index entry. `put`/`get` only move the JSON pointer — they
    /// never touch a blob — so a made-up digest is enough to exercise them.
    fn sample_meta(bucket: &str, key: &str, digest: &str) -> ObjectMeta {
        ObjectMeta {
            bucket: bucket.into(),
            key: key.into(),
            digest: Digest(digest.into()),
            size: 1024,
            etag: ETag(format!("etag-{digest}")),
            content_type: "application/octet-stream".into(),
            last_modified: chrono::Utc::now(),
        }
    }

    /// put → get round-trips the full metadata, and a key containing `/` is
    /// stored flat (exercising the percent-encoded path) without becoming a dir.
    #[tokio::test]
    async fn put_then_get_roundtrips_metadata() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();

        index
            .put(sample_meta("photos", "vacation/beach.jpg", "a1b2c3"))
            .await
            .expect("put should succeed");

        let got = index
            .get("photos", "vacation/beach.jpg")
            .await
            .expect("get should not error")
            .expect("the key must exist after put");
        assert_eq!(got.key, "vacation/beach.jpg", "key round-trips");
        assert_eq!(got.digest, Digest("a1b2c3".into()), "digest round-trips");
        assert_eq!(got.size, 1024, "size round-trips");
        assert_eq!(got.content_type, "application/octet-stream");
    }

    #[tokio::test]
    async fn get_is_none_for_a_missing_key() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();

        let got = index
            .get("photos", "never-put.jpg")
            .await
            .expect("get should not error");
        assert!(
            got.is_none(),
            "a key that was never put must read back as None, not error"
        );
    }

    /// Re-PUT of an existing key replaces the pointer (last-writer-wins), via the
    /// atomic rename over the existing entry.
    #[tokio::test]
    async fn put_overwrite_replaces_the_pointer() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();

        index
            .put(sample_meta("photos", "beach.jpg", "aaaa"))
            .await
            .unwrap();
        index
            .put(sample_meta("photos", "beach.jpg", "bbbb"))
            .await
            .unwrap();

        let got = index
            .get("photos", "beach.jpg")
            .await
            .unwrap()
            .expect("the key must still exist after overwrite");
        assert_eq!(
            got.digest,
            Digest("bbbb".into()),
            "the second put must win — the pointer now names the new blob"
        );
    }

    // ── consistency: dedup + delete + GC (the heart of V3) ──────────────────

    /// Two keys with identical content resolve to ONE blob on disk — dedup is
    /// real, not a promise.
    #[tokio::test]
    async fn dedup_two_keys_share_one_blob() {
        let (_root, store, index) = fresh_full();
        index.create_bucket("photos").await.unwrap();

        let stored = store_bytes(&store, b"identical image bytes").await;
        index
            .put(meta_for("photos", "vacation/beach.jpg", &stored))
            .await
            .unwrap();
        index
            .put(meta_for("photos", "backup/beach-copy.jpg", &stored))
            .await
            .unwrap();

        assert!(
            store.contains(&stored.digest).await,
            "both keys reference the single shared blob"
        );
        // Both pointers resolve, to the same digest.
        let a = index
            .get("photos", "vacation/beach.jpg")
            .await
            .unwrap()
            .unwrap();
        let b = index
            .get("photos", "backup/beach-copy.jpg")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(a.digest, b.digest, "two keys, one digest");
    }

    /// Deleting one of two keys that share a blob must NOT drop the bytes — the
    /// other key still needs them. `delete` is a pointer drop, never an `rm`.
    #[tokio::test]
    async fn deleting_one_of_two_keys_keeps_the_shared_blob() {
        let (_root, store, index) = fresh_full();
        index.create_bucket("photos").await.unwrap();

        let stored = store_bytes(&store, b"shared bytes").await;
        index
            .put(meta_for("photos", "a.jpg", &stored))
            .await
            .unwrap();
        index
            .put(meta_for("photos", "b.jpg", &stored))
            .await
            .unwrap();

        index.delete("photos", "a.jpg").await.unwrap();

        assert!(
            index.get("photos", "a.jpg").await.unwrap().is_none(),
            "the deleted key is gone"
        );
        assert!(
            index.get("photos", "b.jpg").await.unwrap().is_some(),
            "the surviving key still resolves"
        );
        assert!(
            store.contains(&stored.digest).await,
            "the blob must survive while another key references it"
        );
    }

    /// GC reclaims exactly the blobs no live key references — and leaves the
    /// still-referenced ones untouched.
    #[tokio::test]
    async fn gc_reclaims_only_unreferenced_blobs() {
        let (_root, store, index) = fresh_full();
        index.create_bucket("photos").await.unwrap();

        let live = store_bytes(&store, b"still referenced").await;
        let orphan = store_bytes(&store, b"about to be orphaned").await;
        index
            .put(meta_for("photos", "live.txt", &live))
            .await
            .unwrap();
        index
            .put(meta_for("photos", "doomed.txt", &orphan))
            .await
            .unwrap();

        // Orphan the second blob by removing its only pointer.
        index.delete("photos", "doomed.txt").await.unwrap();

        let reclaimed = index.gc().await.expect("gc should run");

        assert_eq!(
            reclaimed, 1,
            "exactly one blob (the orphan) should be reclaimed"
        );
        assert!(
            !store.contains(&orphan.digest).await,
            "the unreferenced blob must be gone"
        );
        assert!(
            store.contains(&live.digest).await,
            "the still-referenced blob must be untouched"
        );
    }

    /// A hard crash (power loss / kill -9) mid-`put` leaves a *partial* JSON in
    /// `tmp/` that the Drop guard never got to clean. GC scans `tmp/` for
    /// in-flight references, so it meets that garbage — and must SKIP it, not
    /// abort the whole sweep. One corrupt temp can't be allowed to freeze
    /// reclamation forever.
    #[tokio::test]
    async fn gc_tolerates_a_corrupt_temp_entry() {
        let (root, store, index) = fresh_full();
        index.create_bucket("photos").await.unwrap();

        // A real, referenced object (also creates the bucket's tmp/ dir).
        let live = store_bytes(&store, b"keep me").await;
        index
            .put(meta_for("photos", "live.txt", &live))
            .await
            .unwrap();

        // Simulate the crash-leftover: truncated JSON in the bucket's tmp/.
        let tmp_dir = root.path().join("index").join("photos").join("tmp");
        tokio::fs::create_dir_all(&tmp_dir).await.unwrap();
        tokio::fs::write(tmp_dir.join("crashed-half.json"), b"{\"bucket\":\"pho")
            .await
            .unwrap();

        let reclaimed = index
            .gc()
            .await
            .expect("gc must skip a corrupt temp, not abort the sweep");

        assert_eq!(
            reclaimed, 0,
            "the only blob is referenced — nothing to reclaim"
        );
        assert!(
            store.contains(&live.digest).await,
            "the live blob must survive a GC run that saw a corrupt temp"
        );
    }

    /// The in-flight-PUT guard: a blob named ONLY by an in-flight `tmp/` entry
    /// (its committed pointer not renamed into place yet) must NOT be reaped —
    /// even though grace is ZERO in tests, so nothing else protects it. This is
    /// the dedup-in-flight window: commit blob → write tmp entry → [GC here] →
    /// rename. Reaping here would delete a blob out from under a committing PUT.
    #[tokio::test]
    async fn gc_keeps_a_blob_referenced_only_by_an_in_flight_temp() {
        let (root, store, index) = fresh_full();
        index.create_bucket("photos").await.unwrap();

        // Blob is durable, but its only reference lives in tmp/ (not renamed).
        let stored = store_bytes(&store, b"committing right now").await;
        let tmp_dir = root.path().join("index").join("photos").join("tmp");
        tokio::fs::create_dir_all(&tmp_dir).await.unwrap();
        let json = serde_json::to_vec(&meta_for("photos", "in-flight.txt", &stored)).unwrap();
        tokio::fs::write(tmp_dir.join("in-flight.txt-abc.json"), json)
            .await
            .unwrap();

        let reclaimed = index.gc().await.expect("gc runs");

        assert_eq!(
            reclaimed, 0,
            "a blob referenced by an in-flight temp must be treated as live"
        );
        assert!(
            store.contains(&stored.digest).await,
            "the committing PUT's blob must survive GC via the tmp-scan"
        );
    }

    // ── listing: the folder illusion + pagination ───────────────────────────

    /// PUT a set of keys under a bucket (synthetic content — listing reads the
    /// pointer, not the blob).
    async fn put_keys(index: &Index, bucket: &str, keys: &[&str]) {
        for (i, key) in keys.iter().enumerate() {
            index
                .put(sample_meta(bucket, key, &format!("d{i}")))
                .await
                .unwrap();
        }
    }

    /// delimiter=`/` collapses keys sharing the next path segment into ONE common
    /// prefix (a fake "folder"); only keys with no further delimiter are listed.
    /// This is kickoff scenario ③.
    #[tokio::test]
    async fn list_delimiter_collapses_into_common_prefixes() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();
        put_keys(
            &index,
            "photos",
            &["a/b/1.txt", "a/b/2.txt", "a/c.txt", "d.txt"],
        )
        .await;

        let page = index
            .list("photos", "a/", Some("/"), None, 100)
            .await
            .unwrap();

        let object_keys: Vec<&str> = page.objects.iter().map(|m| m.key.as_str()).collect();
        let prefixes: Vec<&str> = page.common_prefixes.iter().map(|s| s.as_str()).collect();
        assert_eq!(
            object_keys,
            ["a/c.txt"],
            "only the leaf key under a/ is listed"
        );
        assert_eq!(
            prefixes,
            ["a/b/"],
            "a/b/1 and a/b/2 collapse into the single prefix a/b/"
        );
        assert!(
            page.next_continuation_token.is_none(),
            "the page is not truncated"
        );
    }

    /// Without a delimiter, `prefix` is a pure filter — every matching key is a
    /// listed object, in sorted order, and there are no common prefixes.
    #[tokio::test]
    async fn list_without_delimiter_filters_by_prefix() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();
        put_keys(
            &index,
            "photos",
            &["a/b/1.txt", "a/b/2.txt", "a/c.txt", "d.txt"],
        )
        .await;

        let page = index.list("photos", "a/", None, None, 100).await.unwrap();

        let object_keys: Vec<&str> = page.objects.iter().map(|m| m.key.as_str()).collect();
        assert_eq!(
            object_keys,
            ["a/b/1.txt", "a/b/2.txt", "a/c.txt"],
            "all keys under a/, sorted; d.txt excluded"
        );
        assert!(page.common_prefixes.is_empty(), "no delimiter → no folders");
    }

    /// max_keys + continuation token walk every key exactly once, in sorted
    /// order, with no page exceeding max_keys.
    #[tokio::test]
    async fn list_pagination_walks_every_key_once() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();
        let keys = ["k0", "k1", "k2", "k3", "k4", "k5", "k6"];
        put_keys(&index, "photos", &keys).await;

        let mut seen = Vec::new();
        let mut token: Option<String> = None;
        loop {
            let page = index
                .list("photos", "", None, token.as_deref(), 3)
                .await
                .unwrap();
            assert!(page.objects.len() <= 3, "no page may exceed max_keys");
            seen.extend(page.objects.iter().map(|m| m.key.clone()));
            match page.next_continuation_token {
                Some(t) => token = Some(t),
                None => break,
            }
        }

        let mut expected: Vec<String> = keys.iter().map(|s| s.to_string()).collect();
        expected.sort();
        assert_eq!(
            seen, expected,
            "pagination must visit every key exactly once, in sorted order"
        );
    }

    /// Delimiter AND pagination together — the case the two isolated tests miss.
    /// CommonPrefixes must count toward max_keys and paginate, not bypass the cap.
    #[tokio::test]
    async fn list_delimiter_with_pagination_caps_common_prefixes() {
        let (_root, index) = fresh();
        index.create_bucket("photos").await.unwrap();
        put_keys(&index, "photos", &["a/1", "b/1", "c/1"]).await;

        // 3 folders (a/ b/ c/), max_keys=2 → the first page holds at most 2 of
        // them and must signal there's more.
        let page = index.list("photos", "", Some("/"), None, 2).await.unwrap();

        let on_page = page.objects.len() + page.common_prefixes.len();
        assert!(
            on_page <= 2,
            "objects + common_prefixes must not exceed max_keys (got {on_page})"
        );
        assert!(
            page.next_continuation_token.is_some(),
            "with 3 folders and max_keys=2 the listing is truncated"
        );
    }

    /// Turn any `Debug` error into a `proptest` failure (a shrunk counterexample
    /// beats a bare `unwrap` panic).
    fn fail<E: std::fmt::Debug>(e: E) -> TestCaseError {
        TestCaseError::fail(format!("{e:?}"))
    }

    /// Four distinct contents keyed by a small group id → at most four distinct
    /// blobs, so duplicate groups genuinely share one blob (exercising the
    /// refcount-by-GC logic).
    fn group_bytes(group: u8) -> Vec<u8> {
        format!("content-for-group-{group}").into_bytes()
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(24))]

        /// The full mark-and-sweep refcount law, in BOTH directions: after
        /// deleting an arbitrary subset of keys, a blob is present on disk after a
        /// GC pass **iff** some still-live key references it. Deleting one of two
        /// keys that share a blob keeps the bytes; deleting the last referencing
        /// key lets GC reclaim them.
        ///
        /// This lives in-crate (not in `tests/`) on purpose: `Index::gc_grace()`
        /// is `Duration::ZERO` only under `cfg!(test)`, which is active for the
        /// library's own test build but not for an external integration crate. So
        /// the *reclamation* direction — orphan removed immediately — is only
        /// observable here. The integration test asserts the grace-independent
        /// safety half (`gc_never_reaps_a_referenced_blob`).
        #[test]
        fn prop_gc_reclaims_exactly_unreferenced_blobs(
            entries in prop::collection::hash_map("[a-z0-9/]{1,10}", (0u8..4, any::<bool>()), 0..12),
        ) {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build current-thread runtime");
            rt.block_on(async {
                let (_root, store, index) = fresh_full();
                index.create_bucket("photos").await.map_err(fail)?;

                let mut digest_of_group: std::collections::HashMap<u8, Digest> =
                    std::collections::HashMap::new();
                for (key, (group, _)) in &entries {
                    let stored = store_bytes(&store, &group_bytes(*group)).await;
                    digest_of_group.insert(*group, stored.digest.clone());
                    index.put(meta_for("photos", key, &stored)).await.map_err(fail)?;
                }
                for (key, (_, delete)) in &entries {
                    if *delete {
                        index.delete("photos", key).await.map_err(fail)?;
                    }
                }

                index.gc().await.map_err(fail)?;

                // A group is still referenced iff some non-deleted key used it.
                let referenced: std::collections::HashSet<u8> = entries
                    .values()
                    .filter(|(_, delete)| !*delete)
                    .map(|(group, _)| *group)
                    .collect();

                for (group, digest) in &digest_of_group {
                    let present = store.contains(digest).await;
                    prop_assert_eq!(
                        present,
                        referenced.contains(group),
                        "group {}: blob present must equal 'still referenced'",
                        group
                    );
                }
                Ok::<(), TestCaseError>(())
            })?;
        }
    }
}
