//! V4 — Multipart upload (the S3 protocol) + the multipart ETag.
//!
//! This is the protocol that lets a 5 GB upload survive a flaky network: split
//! it into parts, upload them in parallel and out of order, assemble at the end.
//! An upload is a *session* identified by an `upload_id`; parts are staged until
//! the client `Complete`s (assemble) or `Abort`s (discard).
//!
//! The ETag is the compatibility test, and it's deliberately weird — see
//! `complete`. Get it wrong and the AWS SDK rejects your responses.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::durable::TempEntry;
use crate::error::AppError;
use crate::index::{NewVersion, Precondition};
use crate::index_backend::IndexBackend;
use crate::naming::{Bucket, Key};
use crate::object::{BlobKind, Digest, ETag, ObjectMeta};
use crate::store::Store;
use futures_util::StreamExt;
use md5::Md5;
use serde::{Deserialize, Serialize};
use sha2::Digest as Sha256Digest;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

/// The per-upload metadata persisted next to a session's staged parts.
///
/// Written as `session.json` at [`Multipart::initiate`] and re-read at
/// [`Multipart::complete`], so the finished object lands under the original
/// bucket/key with the content type the client declared — even if the process
/// restarts between the two calls.
#[derive(Debug, Serialize, Deserialize)]
struct UploadSession {
    bucket: Bucket,
    key: Key,
    content_type: String,
}

impl UploadSession {
    fn new(bucket: Bucket, key: Key, content_type: impl Into<String>) -> Self {
        Self {
            bucket,
            key,
            content_type: content_type.into(),
        }
    }
}

/// Owns in-progress multipart uploads: their staging areas and the assemble /
/// abort logic. Writes finished objects through V1 (`store`) and V3 (`index`).
pub struct Multipart {
    root: PathBuf,
    store: Arc<Store>,
    index: Arc<IndexBackend>,
}

/// What `UploadPart` hands back — the per-part ETag (the part's MD5) the client
/// must echo in `CompleteMultipartUpload` so we can validate the assembly.
pub struct PartETag {
    /// The part's 1-based index (`1..=10_000`, S3's `MAX_PART_NUMBER`); its
    /// position when the parts are concatenated at completion.
    pub part_number: u32,
    /// The part's ETag: the hex-encoded MD5 of exactly this part's bytes.
    pub etag: ETag,
}

impl Multipart {
    const MAX_PART_NUMBER: u32 = 10_000;
    const SESSION_FILE: &str = "session.json";
    const MD5_HEX_LEN: usize = 16;

    /// Open the multipart subsystem, creating its `uploads/` staging root under
    /// `root` if needed.
    ///
    /// Returns an [`Arc`] so the HTTP layer can share one instance across
    /// requests; it borrows the same [`Store`] (V1 blobs) and [`IndexBackend`]
    /// (V3 metadata) a completed upload is written through.
    ///
    /// # Errors
    /// Propagates any `std::io::Error` from creating the staging directory.
    pub fn open(
        root: impl AsRef<Path>,
        store: Arc<Store>,
        index: Arc<IndexBackend>,
    ) -> std::io::Result<Arc<Self>> {
        let root = root.as_ref().join("uploads");
        std::fs::create_dir_all(&root)?;
        Ok(Arc::new(Self { root, store, index }))
    }

    /// `CreateMultipartUpload` — open a session and return its `upload_id`.
    ///
    /// Allocates a fresh UUID `upload_id`, creates its staging directory, and
    /// persists the target bucket/key/content type as the upload's session. No
    /// parts exist yet; the client stages them with [`Self::upload_part`] and
    /// finalizes with [`Self::complete`].
    ///
    /// # Errors
    /// - [`AppError::InvalidRequest`] if `bucket` is not a valid bucket name.
    /// - [`AppError::NoSuchBucket`] if the bucket does not exist.
    /// - I/O / serialization errors while creating the staging dir or writing
    ///   the session file.
    #[tracing::instrument(
        skip(self, content_type),
        fields(upload_id = tracing::field::Empty),
    )]
    pub async fn initiate(
        &self,
        bucket: &Bucket,
        key: &Key,
        content_type: String,
    ) -> Result<String, AppError> {
        self.index.ensure_bucket(bucket).await?;

        let upload_id = uuid::Uuid::new_v4().to_string();
        let staging_dir = self.root.join(&upload_id);
        tokio::fs::create_dir_all(&staging_dir).await?;

        let session = UploadSession::new(bucket.clone(), key.clone(), content_type);
        tokio::fs::write(self.session_path(&upload_id), serde_json::to_vec(&session)?).await?;

        tracing::Span::current().record("upload_id", upload_id.as_str());
        metrics::counter!(crate::metrics::MULTIPART_INITIATED_TOTAL).increment(1);
        metrics::gauge!(crate::metrics::MULTIPART_OPEN_SESSIONS).increment(1.0);
        Ok(upload_id)
    }

    /// The path to a session's `session.json` file.
    #[inline]
    fn session_path(&self, upload_id: &str) -> PathBuf {
        self.root.join(upload_id).join(Self::SESSION_FILE)
    }

    /// The path to a part's staged file.
    #[inline]
    fn part_path(&self, upload_id: &str, part_number: u32) -> PathBuf {
        self.root
            .join(upload_id)
            .join(format!("{part_number:05}.part"))
    }

    /// `UploadPart` — stream one part into its staging file and return its MD5
    /// ETag.
    ///
    /// MD5-hashes the body as it writes, enforcing `max_part_size` mid-stream. A
    /// [`TempEntry`] guard makes the write all-or-nothing: a rejected or
    /// interrupted part leaves no half-written file behind. Re-uploading the same
    /// `part_number` overwrites the previously staged part. The returned
    /// [`PartETag`] is what the client must echo back in [`Self::complete`].
    ///
    /// # Errors
    /// - [`AppError::InvalidRequest`] if `part_number` is outside
    ///   `1..=10_000` (S3's `MAX_PART_NUMBER`).
    /// - [`AppError::NoSuchUpload`] if `upload_id` names no live session.
    /// - [`AppError::EntityTooLarge`] if the streamed bytes exceed
    ///   `max_part_size`.
    /// - [`AppError::Other`] on a body-stream or file-write error.
    #[tracing::instrument(
        skip(self, body),
        fields(size = tracing::field::Empty, etag = tracing::field::Empty),
    )]
    pub async fn upload_part<S>(
        &self,
        upload_id: &str,
        part_number: u32,
        body: S,
        max_part_size: u64,
    ) -> Result<PartETag, AppError>
    where
        S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin,
    {
        use tokio::fs as tfs;

        if !(1..=Self::MAX_PART_NUMBER).contains(&part_number) {
            return Err(AppError::InvalidRequest(format!(
                "part_number must be between 1 and {}, got {part_number}",
                Self::MAX_PART_NUMBER,
            )));
        }

        self.staging_dir(upload_id).await?;
        let part_path = self.part_path(upload_id, part_number);

        let mut part_guard = TempEntry::new(part_path);
        let mut part_file = tfs::File::create(part_guard.path()).await?;
        let mut hasher = Md5::new();
        let mut total_size = 0u64;
        let mut body = body;

        // Time only the streaming loop so throughput reflects the transfer, not
        // session lookup or hashing setup. `total_size` is counted from the
        // bytes we actually receive — never a client-supplied Content-Length.
        let started = tokio::time::Instant::now();
        loop {
            match body.next().await {
                None => break,
                Some(Ok(bytes)) => {
                    total_size += bytes.len() as u64;
                    if total_size > max_part_size {
                        return Err(AppError::EntityTooLarge);
                    }
                    hasher.update(&bytes);
                    part_file
                        .write_all(&bytes)
                        .await
                        .map_err(|e| AppError::Other(e.into()))?;
                }
                Some(Err(err)) => return Err(AppError::Other(err.into())),
            }
        }

        let etag = ETag(hex::encode(hasher.finalize()));
        part_guard.disarm();

        let span = tracing::Span::current();
        span.record("size", total_size);
        span.record("etag", etag.as_str());

        metrics::counter!(crate::metrics::MULTIPART_PARTS_UPLOADED_TOTAL).increment(1);
        metrics::histogram!(crate::metrics::MULTIPART_PART_BYTES).record(total_size as f64);
        let elapsed = started.elapsed().as_secs_f64();
        if elapsed > 0.0 {
            metrics::histogram!(crate::metrics::MULTIPART_PART_THROUGHPUT)
                .record(total_size as f64 / elapsed);
        }
        Ok(PartETag { part_number, etag })
    }

    /// Resolve a session's staging directory, or [`AppError::NoSuchUpload`] if
    /// the `upload_id` names no live session.
    async fn staging_dir(&self, upload_id: &str) -> Result<PathBuf, AppError> {
        let staging_dir = self.root.join(upload_id);
        if !tokio::fs::try_exists(&staging_dir).await? {
            return Err(AppError::NoSuchUpload);
        }
        Ok(staging_dir)
    }

    /// `CompleteMultipartUpload` — assemble the listed parts in order into one
    /// object, commit it (V1), index it (V3), and return the final S3 ETag.
    ///
    /// Each listed part's staged MD5 is re-verified against the client-supplied
    /// [`PartETag`] before its bytes are appended, and the final ETag is the S3
    /// multipart form `hex(md5(concat(raw part md5s)))-N` — deliberately *not*
    /// the digest of the assembled bytes (see the module docs). On any rejection
    /// the half-assembled temp file is dropped, so no orphan blob is committed.
    ///
    /// # Errors
    /// - [`AppError::NoSuchUpload`] if the session or its `session.json` is gone.
    /// - [`AppError::InvalidRequest`] for an empty or duplicate-numbered part
    ///   list, a part that was never staged, or a part whose staged MD5 or ETag
    ///   encoding does not match the client's claim.
    /// - I/O / serialization errors while reading parts or committing the object.
    #[tracing::instrument(
        skip(self, parts),
        fields(
            bucket = tracing::field::Empty,
            key = tracing::field::Empty,
            size = tracing::field::Empty,
            part_count = parts.len(),
        ),
    )]
    pub async fn complete(
        &self,
        upload_id: &str,
        mut parts: Vec<PartETag>,
    ) -> Result<ObjectMeta, AppError> {
        use tokio::fs as tfs;

        parts.sort_by_key(|part| part.part_number);
        verify_parts_digest(&parts)?;

        let staging_dir = self.staging_dir(upload_id).await?;
        let session = {
            let session_path = staging_dir.join(Self::SESSION_FILE);
            if !tfs::try_exists(&session_path).await? {
                return Err(AppError::NoSuchUpload);
            }
            let bytes = tfs::read(session_path).await?;
            serde_json::from_slice::<UploadSession>(&bytes)?
        };

        let part_count = parts.len();

        let mut temp = TempEntry::unique_in(self.store.tmp_dir(), "multipart");
        let mut tmp_file = tfs::File::create(temp.path()).await?;
        let mut sha_hasher = sha2::Sha256::new();
        let mut total_size = 0u64;
        let mut part_md5_bytes = Vec::with_capacity(part_count * Self::MD5_HEX_LEN);
        let mut buf = vec![0u8; 64 * 1024];

        for PartETag { part_number, etag } in &parts {
            let part_path = self.part_path(upload_id, *part_number);
            if !tfs::try_exists(&part_path).await? {
                return Err(AppError::InvalidRequest(format!(
                    "no staged part {part_number}",
                )));
            }

            let mut part_file = tfs::File::open(&part_path).await?;
            let mut part_hasher = Md5::new();

            loop {
                let n = part_file.read(&mut buf).await?;
                if n == 0 {
                    break;
                }
                let chunk = &buf[..n];
                part_hasher.update(chunk);
                sha_hasher.update(chunk);
                total_size += n as u64;
                tmp_file
                    .write_all(chunk)
                    .await
                    .map_err(|e| AppError::Other(e.into()))?;
            }

            let decoded_etag = {
                let staged_etag = hex::encode(part_hasher.finalize());
                if staged_etag != etag.as_str() {
                    return Err(AppError::InvalidRequest(format!(
                        "part {part_number} etag mismatch: client {} != staged {staged_etag}",
                        etag.as_str(),
                    )));
                }

                let decoded = hex::decode(etag.as_str()).map_err(|_| {
                    AppError::InvalidRequest(format!("invalid etag hex for part {part_number}"))
                })?;

                if decoded.len() != Self::MD5_HEX_LEN {
                    return Err(AppError::InvalidRequest(format!(
                        "part {part_number} etag must be {} bytes of md5 hex",
                        Self::MD5_HEX_LEN,
                    )));
                }
                decoded
            };

            part_md5_bytes.extend(decoded_etag);
        }

        tmp_file.sync_all().await?;

        let digest = Digest(hex::encode(sha_hasher.finalize()));
        let etag = ETag(format!(
            "{}-{}",
            hex::encode(Md5::digest(&part_md5_bytes)),
            part_count
        ));

        self.store.commit_temp(temp.path(), &digest).await?;
        temp.disarm();

        let version = NewVersion {
            digest,
            etag,
            size: total_size,
            content_type: session.content_type,
            blob_kind: BlobKind::Whole,
        };
        let meta = self
            .index
            .put(&session.bucket, &session.key, version, Precondition::None)
            .await?;
        tfs::remove_dir_all(&staging_dir).await?;

        let live = meta.latest_live().ok_or(AppError::NoSuchKey)?;
        let span = tracing::Span::current();
        span.record("bucket", meta.bucket.as_str());
        span.record("key", meta.key.as_str());
        span.record("size", live.size);

        metrics::counter!(crate::metrics::MULTIPART_COMPLETED_TOTAL).increment(1);
        metrics::gauge!(crate::metrics::MULTIPART_OPEN_SESSIONS).decrement(1.0);
        metrics::histogram!(crate::metrics::MULTIPART_OBJECT_BYTES).record(live.size as f64);
        tracing::info!(size = live.size, part_count, "multipart upload completed");
        Ok(meta)
    }

    /// `AbortMultipartUpload` — discard a session and reclaim its staged parts.
    ///
    /// # Errors
    /// [`AppError::NoSuchUpload`] if `upload_id` names no live session, plus any
    /// I/O error while removing the staging directory.
    #[tracing::instrument(skip(self))]
    pub async fn abort(&self, upload_id: &str) -> Result<(), AppError> {
        use tokio::fs as tfs;
        let staging_dir = self.staging_dir(upload_id).await?;
        if !tfs::try_exists(&staging_dir).await? {
            return Err(AppError::NoSuchUpload);
        }
        tfs::remove_dir_all(&staging_dir).await?;

        // The session is gone; release its slot on the open-session gauge.
        metrics::counter!(crate::metrics::MULTIPART_ABORTED_TOTAL).increment(1);
        metrics::gauge!(crate::metrics::MULTIPART_OPEN_SESSIONS).decrement(1.0);
        Ok(())
    }
}

/// Validate a part list already sorted by `part_number` before assembly begins:
/// reject an empty list or duplicate part numbers.
///
/// # Errors
/// [`AppError::InvalidRequest`] if `sorted_parts` is empty or contains two
/// entries with the same `part_number`.
fn verify_parts_digest(sorted_parts: &[PartETag]) -> Result<(), AppError> {
    if sorted_parts.is_empty() {
        return Err(AppError::InvalidRequest(
            "complete requires at least one part".into(),
        ));
    }

    if sorted_parts
        .windows(2)
        .any(|window| window[0].part_number == window[1].part_number)
    {
        return Err(AppError::InvalidRequest(
            "duplicate part numbers in complete request".into(),
        ));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::Index;
    use bytes::Bytes;
    use futures_util::stream;
    use tempfile::TempDir;
    use tokio::io::AsyncReadExt;

    const BUCKET: &str = "photos";

    fn b(name: &str) -> Bucket {
        Bucket::from_trusted(name)
    }
    fn k(name: &str) -> Key {
        Key::from_trusted(name)
    }

    /// A full V4 stack (store + index + multipart) over a throwaway data dir.
    /// The `TempDir` is returned so tests can peek at the on-disk staging layout
    /// and so the whole tree is wiped on drop.
    fn fresh() -> (TempDir, Arc<Store>, Arc<Index>, Arc<Multipart>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        let index = Index::open(root.path(), store.clone()).expect("open index");
        let backend = Arc::new(IndexBackend::local(index.clone()));
        let multipart =
            Multipart::open(root.path(), store.clone(), backend).expect("open multipart");
        (root, store, index, multipart)
    }

    /// A one-chunk body stream, the shape `upload_part` consumes.
    fn body(bytes: &[u8]) -> impl futures_util::Stream<Item = Result<Bytes, axum::Error>> + Unpin {
        stream::iter(vec![Ok(Bytes::copy_from_slice(bytes))])
    }

    /// Stream one numbered part into a session with a generous size cap.
    async fn upload(mp: &Multipart, id: &str, part_number: u32, bytes: &[u8]) -> PartETag {
        mp.upload_part(id, part_number, body(bytes), 1 << 20)
            .await
            .expect("upload_part should succeed")
    }

    /// Read a committed blob back by digest — how we prove the assembled bytes.
    async fn read_blob(store: &Store, digest: &Digest) -> Vec<u8> {
        let mut file = store.open_blob(digest).await.expect("open committed blob");
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes).await.expect("read blob");
        bytes
    }

    /// On-disk staging dir for a session — used to assert reclamation.
    fn staging_path(root: &TempDir, upload_id: &str) -> PathBuf {
        root.path().join("uploads").join(upload_id)
    }

    /// The reference multipart ETag, computed independently of the impl:
    /// `hex(md5(concat(md5(part_i) for i in order))) + "-N"`. The MD5s are
    /// concatenated as RAW 16-byte digests, in part-number order — the exact
    /// thing that must match `aws s3 cp`.
    fn expected_multipart_etag(parts_in_order: &[&[u8]]) -> String {
        let mut concat = Vec::new();
        for part in parts_in_order {
            concat.extend_from_slice(Md5::digest(part).as_slice());
        }
        format!(
            "{}-{}",
            hex::encode(Md5::digest(&concat)),
            parts_in_order.len()
        )
    }

    // ── initiate / upload_part validation ───────────────────────────────────

    #[tokio::test]
    async fn initiate_rejects_a_missing_bucket() {
        let (_root, _store, _index, mp) = fresh();
        // No bucket created — `ensure_bucket` must reject before a session exists.
        assert!(matches!(
            mp.initiate(&b("nope"), &k("k"), "text/plain".into()).await,
            Err(AppError::NoSuchBucket)
        ));
    }

    #[tokio::test]
    async fn upload_part_rejects_out_of_range_part_numbers() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        for bad in [0, Multipart::MAX_PART_NUMBER + 1] {
            let outcome = mp.upload_part(&id, bad, body(b"x"), 1 << 20).await;
            assert!(
                matches!(outcome, Err(AppError::InvalidRequest(_))),
                "part_number {bad} is outside 1..=10000 and must be rejected"
            );
        }
    }

    #[tokio::test]
    async fn upload_part_on_unknown_session_is_no_such_upload() {
        let (_root, _store, _index, mp) = fresh();
        assert!(matches!(
            mp.upload_part("ghost-id", 1, body(b"x"), 1 << 20).await,
            Err(AppError::NoSuchUpload)
        ));
    }

    #[tokio::test]
    async fn upload_part_etag_is_the_part_md5() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        let bytes = b"one part's worth of bytes";
        let part = upload(&mp, &id, 1, bytes).await;
        assert_eq!(
            part.etag.0,
            hex::encode(Md5::digest(bytes)),
            "a part's ETag is the hex MD5 of its bytes"
        );
    }

    #[tokio::test]
    async fn upload_part_over_the_cap_is_rejected_and_leaves_no_part_file() {
        let (root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        let outcome = mp.upload_part(&id, 1, body(&[b'x'; 100]), 50).await;
        assert!(
            matches!(outcome, Err(AppError::EntityTooLarge)),
            "a part exceeding max_part_size must be EntityTooLarge"
        );
        assert!(
            !staging_path(&root, &id).join("00001.part").exists(),
            "a rejected part must not leave a half-written file behind"
        );
    }

    #[tokio::test]
    async fn complete_assembles_out_of_order_parts_in_part_number_order() {
        let (_root, store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("big.txt"), "text/plain".into())
            .await
            .unwrap();

        // Upload part 2 BEFORE part 1 — arrival order must not affect assembly.
        let p2 = upload(&mp, &id, 2, b"<part-two>").await;
        let p1 = upload(&mp, &id, 1, b"<part-one>").await;

        // Hand the parts to complete in reverse too, to prove it sorts.
        let meta = mp.complete(&id, vec![p2, p1]).await.expect("complete");
        let live = meta.latest_live().expect("completed object must be live");

        let assembled = read_blob(&store, &live.digest).await;
        assert_eq!(
            assembled, b"<part-one><part-two>",
            "parts must concatenate in part-number order, not arrival order"
        );
        assert_eq!(live.size, assembled.len() as u64, "size is the byte count");
    }

    #[tokio::test]
    async fn complete_computes_the_multipart_etag() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        let a: &[u8] = b"aaaaaaaa";
        let b: &[u8] = b"bbbbbbbbbbbb";
        let p1 = upload(&mp, &id, 1, a).await;
        let p2 = upload(&mp, &id, 2, b).await;

        let meta = mp.complete(&id, vec![p1, p2]).await.expect("complete");
        let live = meta.latest_live().expect("completed object must be live");

        assert_eq!(
            live.etag.0,
            expected_multipart_etag(&[a, b]),
            "multipart ETag = hex(md5(concat(raw part md5s)))-N"
        );
        assert!(
            live.etag.0.ends_with("-2"),
            "the -N suffix carries the part count"
        );
        assert_ne!(
            live.etag.0,
            hex::encode(Md5::digest([a, b].concat())),
            "the multipart ETag is NOT the plain MD5 of the assembled bytes"
        );
    }

    #[tokio::test]
    async fn complete_indexes_the_object_and_removes_the_session() {
        let (root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("a/b.txt"), "application/json".into())
            .await
            .unwrap();
        let p1 = upload(&mp, &id, 1, b"hello ").await;
        let p2 = upload(&mp, &id, 2, b"world").await;

        let meta = mp.complete(&id, vec![p1, p2]).await.expect("complete");
        let live = meta.latest_live().expect("completed object must be live");

        let indexed = index
            .get(&b(BUCKET), &k("a/b.txt"))
            .await
            .unwrap()
            .expect("completed object must be indexed")
            .latest_live()
            .expect("indexed object must be live");
        assert_eq!(indexed.digest, live.digest);
        assert_eq!(indexed.etag, live.etag);
        assert_eq!(
            indexed.content_type, "application/json",
            "content_type round-trips from initiate"
        );

        // The staging area is reclaimed; the session no longer exists.
        assert!(
            !staging_path(&root, &id).exists(),
            "complete must delete the staging dir"
        );
        assert!(
            mp.complete(&id, vec![]).await.is_err(),
            "a completed session cannot be completed again"
        );
    }

    #[tokio::test]
    async fn complete_rejects_a_client_etag_that_does_not_match_the_staged_part() {
        let (_root, store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();
        upload(&mp, &id, 1, b"real bytes").await;

        // Client claims a well-formed but wrong MD5 for part 1.
        let lying = PartETag {
            part_number: 1,
            etag: ETag("0".repeat(32)),
        };
        let outcome = mp.complete(&id, vec![lying]).await;

        assert!(
            matches!(outcome, Err(AppError::InvalidRequest(_))),
            "a part whose staged MD5 differs from the client's claim must be rejected"
        );
        assert!(
            index.get(&b(BUCKET), &k("k")).await.unwrap().is_none(),
            "a rejected complete must not index a partial object"
        );
        // The half-assembled temp must be cleaned up, so no orphan blob lingers.
        let digest = Digest(hex::encode(sha2::Sha256::digest(b"real bytes")));
        assert!(
            !store.contains(&digest).await,
            "no blob should be committed on a rejected complete"
        );
    }

    #[tokio::test]
    async fn complete_rejects_a_part_that_was_never_staged() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();
        let p1 = upload(&mp, &id, 1, b"only part one").await;
        // Fabricate a part 2 the server never received.
        let ghost = PartETag {
            part_number: 2,
            etag: ETag(hex::encode(Md5::digest(b"never uploaded"))),
        };

        assert!(matches!(
            mp.complete(&id, vec![p1, ghost]).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn complete_rejects_an_empty_part_list() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        assert!(matches!(
            mp.complete(&id, vec![]).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn complete_rejects_duplicate_part_numbers() {
        let (_root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();
        let p1 = upload(&mp, &id, 1, b"first").await;
        let dup = PartETag {
            part_number: 1,
            etag: p1.etag.clone(),
        };

        assert!(matches!(
            mp.complete(&id, vec![p1, dup]).await,
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[tokio::test]
    async fn complete_on_unknown_session_is_no_such_upload() {
        let (_root, _store, _index, mp) = fresh();
        let part = PartETag {
            part_number: 1,
            etag: ETag(hex::encode(Md5::digest(b"x"))),
        };
        assert!(matches!(
            mp.complete("ghost-id", vec![part]).await,
            Err(AppError::NoSuchUpload)
        ));
    }

    #[tokio::test]
    async fn retrying_a_part_overwrites_the_previous_bytes() {
        let (_root, store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();

        // First attempt at part 1 is superseded by a retry with new content.
        upload(&mp, &id, 1, b"STALE-first-try").await;
        let good = upload(&mp, &id, 1, b"fresh").await;

        let meta = mp.complete(&id, vec![good]).await.expect("complete");
        let live = meta.latest_live().expect("completed object must be live");
        assert_eq!(
            read_blob(&store, &live.digest).await,
            b"fresh",
            "a re-uploaded part N must overwrite the earlier staged part N"
        );
    }

    #[tokio::test]
    async fn abort_reclaims_staged_parts_and_indexes_nothing() {
        let (root, _store, index, mp) = fresh();
        index.create_bucket(&b(BUCKET)).await.unwrap();
        let id = mp
            .initiate(&b(BUCKET), &k("k"), "text/plain".into())
            .await
            .unwrap();
        upload(&mp, &id, 1, b"staged but never completed").await;

        mp.abort(&id).await.expect("abort");

        assert!(
            !staging_path(&root, &id).exists(),
            "abort must delete the staging dir"
        );
        assert!(
            index.get(&b(BUCKET), &k("k")).await.unwrap().is_none(),
            "an aborted upload must never produce an index entry"
        );
        assert!(
            matches!(
                mp.upload_part(&id, 2, body(b"x"), 1 << 20).await,
                Err(AppError::NoSuchUpload)
            ),
            "the session is gone after abort"
        );
    }

    #[tokio::test]
    async fn abort_on_unknown_session_is_no_such_upload() {
        let (_root, _store, _index, mp) = fresh();
        assert!(matches!(
            mp.abort("ghost-id").await,
            Err(AppError::NoSuchUpload)
        ));
    }
}
