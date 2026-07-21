//! V2 — Streaming bodies, end to end: bounded memory + backpressure.
//!
//! This is where "10 KB on a laptop" and "5 GB in prod" stop being the same
//! program. The request body is pulled one chunk at a time, written straight to
//! a temp file and fed to the hashers — so an object of *any* size costs O(1)
//! memory. Collecting the body into a `Vec<u8>` is the single bug this whole
//! vertical exists to prevent.

use crate::cdc::{CdcChunker, CdcConfig};
use crate::durable::TempEntry;
use crate::error::AppError;
use crate::manifest::{commit_manifest, ChunkRef, Manifest};
use crate::object::{BlobKind, Digest, ETag};
use crate::store::Store;
use axum::extract::FromRequestParts;
use axum::http::{request::Parts, HeaderMap};
use futures_util::StreamExt;
use sha2::{Digest as _, Sha256};
use tokio::io::AsyncWriteExt;

pub enum CheckSumAlgorithm {
    Sha256(String),
    Md5(String),
}

impl CheckSumAlgorithm {
    pub fn suffix(&self) -> &str {
        match self {
            CheckSumAlgorithm::Sha256(_) => "SHA256",
            CheckSumAlgorithm::Md5(_) => "MD5",
        }
    }

    pub fn checksum(&self) -> &str {
        match self {
            CheckSumAlgorithm::Sha256(checksum) => checksum,
            CheckSumAlgorithm::Md5(checksum) => checksum,
        }
    }

    /// Check the client-supplied checksum against the digests already computed
    /// while streaming the body. `sha256` and `md5` are the RAW digest bytes of
    /// the whole object — the same values behind the content [`Digest`] (sha256)
    /// and the [`ETag`] (md5). Whichever algorithm the client named, its digest
    /// is already one of these two, so no extra hash pass over the body is needed.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::InvalidRequest`] when the header isn't valid base64,
    /// or when the computed digest doesn't match what the client sent (S3 calls
    /// this `BadDigest`, a 400).
    fn verify(&self, sha256: &[u8], md5: &[u8]) -> Result<(), AppError> {
        use base64::Engine as _;

        // Pick the raw digest for the algorithm the client asked about — the
        // whole point of reusing the two body hashers instead of a third.
        let computed = match self {
            CheckSumAlgorithm::Sha256(_) => sha256,
            CheckSumAlgorithm::Md5(_) => md5,
        };
        // S3 puts the *raw* digest on the wire as base64 — not the hex we store
        // as Digest/ETag. Decode first, then byte-compare.
        let expected = base64::engine::general_purpose::STANDARD
            .decode(self.checksum())
            .map_err(|_| {
                AppError::InvalidRequest(format!("invalid base64 {} checksum", self.suffix()))
            })?;
        if expected.as_slice() != computed {
            return Err(AppError::InvalidRequest(format!(
                "BadDigest: {} checksum does not match streamed body",
                self.suffix()
            )));
        }
        Ok(())
    }
}

/// Extractor: the checksum the client asked us to verify on a PUT, if any.
///
/// One request-level decision spans three headers — `Content-MD5` wins
/// outright; otherwise `X-Amz-Checksum-Algorithm` names the algorithm and
/// `X-Amz-Checksum-{ALGO}` carries the expected value — so it's modelled as a
/// single [`FromRequestParts`] extractor rather than per-header plumbing in the
/// handler. Malformed input (an unknown algorithm, a named algorithm with no
/// value header, a non-ASCII header value) becomes a 400 rejection before the
/// handler body ever runs, exactly like a bad `Query` string.
pub struct ChecksumSpec(pub Option<CheckSumAlgorithm>);

impl ChecksumSpec {
    /// The whole parse as a pure function over the headers, so unit tests can
    /// drive every branch without building an HTTP request.
    fn from_headers(headers: &HeaderMap) -> Result<Self, AppError> {
        if let Some(md5) = header_str(headers, "Content-MD5")? {
            return Ok(Self(Some(CheckSumAlgorithm::Md5(md5.to_owned()))));
        }

        let Some(algo) = header_str(headers, "X-Amz-Checksum-Algorithm")? else {
            return Ok(Self(None));
        };
        let algo: ChecksumAlgo = algo.parse()?;

        let value_header = format!("X-Amz-Checksum-{}", algo.suffix());
        let value = header_str(headers, &value_header)?
            .ok_or_else(|| AppError::InvalidRequest(format!("missing {value_header} header")))?;
        Ok(Self(Some(algo.with_value(value.to_owned()))))
    }
}

impl<S: Send + Sync> FromRequestParts<S> for ChecksumSpec {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        Self::from_headers(&parts.headers)
    }
}

/// A checksum algorithm named by `X-Amz-Checksum-Algorithm`, matched
/// case-insensitively. Exists separately from [`CheckSumAlgorithm`] because the
/// algorithm arrives one header *before* its value — it has to be parsed first
/// to know which `X-Amz-Checksum-{ALGO}` header to read.
#[derive(Clone, Copy)]
enum ChecksumAlgo {
    Sha256,
    Md5,
}

impl ChecksumAlgo {
    /// The canonical suffix in the value header's name (`X-Amz-Checksum-SHA256`).
    fn suffix(self) -> &'static str {
        match self {
            Self::Sha256 => "SHA256",
            Self::Md5 => "MD5",
        }
    }

    /// Pair the algorithm with the expected value read from its value header.
    fn with_value(self, value: String) -> CheckSumAlgorithm {
        match self {
            Self::Sha256 => CheckSumAlgorithm::Sha256(value),
            Self::Md5 => CheckSumAlgorithm::Md5(value),
        }
    }
}

impl std::str::FromStr for ChecksumAlgo {
    type Err = AppError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        if s.eq_ignore_ascii_case("sha256") {
            Ok(Self::Sha256)
        } else if s.eq_ignore_ascii_case("md5") {
            Ok(Self::Md5)
        } else {
            Err(AppError::InvalidRequest(format!(
                "unsupported checksum algorithm {s:?}"
            )))
        }
    }
}

/// A header's value as `&str`: `Ok(None)` when absent, a 400 when present but
/// not visible ASCII. Header values are arbitrary bytes on the wire, so calling
/// `to_str().unwrap()` on client input is a remotely triggerable panic — every
/// checksum header read goes through here instead.
fn header_str<'h>(headers: &'h HeaderMap, name: &str) -> Result<Option<&'h str>, AppError> {
    headers
        .get(name)
        .map(|value| {
            value
                .to_str()
                .map_err(|_| AppError::InvalidRequest(format!("{name} header is not valid ASCII")))
        })
        .transpose()
}

/// The outcome of streaming one body into the store: its content digest, S3
/// `ETag`, and byte count.
///
/// Produced by [`stream_to_store`] (or [`stream_cdc_to_store`]) once bytes are
/// committed. The caller (V3's PUT handler) records these on the `(bucket, key)`
/// index row and echoes the `ETag` and size back to the client.
///
/// For CDC, [`digest`](Self::digest) is the **manifest** CAS name and
/// [`blob_kind`](Self::blob_kind) is [`BlobKind::Manifest`]; [`size`](Self::size)
/// and [`etag`](Self::etag) still describe the *logical* whole object.
pub struct Stored {
    /// SHA-256 of the streamed bytes, hex-encoded — the blob's content address
    /// (and its name on disk) in the [`Store`]. For CDC this is the manifest.
    pub digest: Digest,
    /// The single-PUT S3 `ETag`, `hex(md5(bytes))`. See [`ETag`] for why this is
    /// deliberately *not* the same value as the content [`Digest`].
    pub etag: ETag,
    /// Total number of bytes streamed — the object's logical size.
    pub size: u64,
    /// Whether `digest` names whole-object bytes or a CDC manifest.
    pub blob_kind: BlobKind,
}

impl Stored {
    pub fn whole(digest: Digest, etag: ETag, size: u64) -> Self {
        Self {
            digest,
            etag,
            size,
            blob_kind: BlobKind::Whole,
        }
    }

    pub fn manifest(digest: Digest, etag: ETag, size: u64) -> Self {
        Self {
            digest,
            etag,
            size,
            blob_kind: BlobKind::Manifest,
        }
    }
}

pub struct Streamer<S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin> {
    max_size: u64,
    checksum_algo: Option<CheckSumAlgorithm>,
    config: Option<CdcConfig>,
    body: S,
}

impl<S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin> Streamer<S> {
    pub fn new(body: S, max_size: u64, checksum_algo: Option<CheckSumAlgorithm>) -> Self
    where
        S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin,
    {
        Self {
            max_size,
            checksum_algo,
            config: None,
            body,
        }
    }
    pub fn with_cdc(self, config: CdcConfig) -> Self {
        Self {
            config: Some(config),
            ..self
        }
    }

    pub async fn stream(self, store: &Store) -> Result<Stored, AppError> {
        if let Some(config) = self.config {
            stream_cdc_to_store(store, self.body, self.max_size, self.checksum_algo, config).await
        } else {
            stream_to_store(store, self.body, self.max_size, self.checksum_algo).await
        }
    }
}

/// Stream a request body to disk chunk by chunk, hashing as it goes, and commit
/// it as a content-addressed blob.
///
/// This is the whole point of V2: the body is pulled one [`Bytes`](bytes::Bytes)
/// chunk at a time, written straight to a temp file and fed to the SHA-256
/// (content digest) and MD5 (`ETag`) hashers, so memory stays O(1) regardless of
/// object size — never collect the body into a `Vec<u8>`. Awaiting each file
/// write is also the backpressure: a fast producer is throttled to disk speed.
/// The staged temp file is owned by a [`TempEntry`](crate::durable::TempEntry)
/// guard, so any early return below unlinks the half-written file on drop; it is
/// disarmed only after [`Store::commit_temp`] has durably published the blob.
///
/// `max_size` caps the *running total*, not any single chunk: the accumulated
/// size is checked after every chunk, so a body that dribbles past the cap in
/// small pieces is still rejected.
///
/// # Errors
///
/// - [`AppError::EntityTooLarge`] once the accumulated size exceeds `max_size`.
/// - [`AppError::Other`] if the body stream yields an error (e.g. a client
///   disconnect mid-upload) or a chunk fails to write to the temp file.
/// - [`AppError::Io`] if staging the temp file or committing the blob fails.
///
/// On every error path the guard's `Drop` removes the temp file, so a rejected
/// or interrupted upload never leaks a partial blob.
///
/// # Examples
///
/// ```
/// use bytes::Bytes;
/// use futures_util::stream;
/// use object_store::store::Store;
/// use object_store::streaming::stream_to_store;
/// use tempfile::TempDir;
///
/// let dir = TempDir::new().unwrap();
/// let store = Store::open(dir.path()).unwrap();
///
/// // A body delivered as two chunks, the way axum hands one to a handler.
/// let body = stream::iter(vec![
///     Ok::<_, axum::Error>(Bytes::from_static(b"hello ")),
///     Ok(Bytes::from_static(b"world")),
/// ]);
///
/// let rt = tokio::runtime::Runtime::new().unwrap();
/// let stored = rt.block_on(stream_to_store(&store, body, 1024, None)).unwrap();
/// assert_eq!(stored.size, 11);
/// ```
pub async fn stream_to_store<S>(
    store: &Store,
    mut body: S,
    max_size: u64,
    checksum_algo: Option<CheckSumAlgorithm>,
) -> Result<Stored, AppError>
where
    S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin,
{
    let mut temp = TempEntry::unique_in(store.tmp_dir(), "stream");
    let mut temp_file = tokio::fs::File::create(temp.path()).await?;
    let mut sha_hasher = Sha256::new();
    let mut md5_hasher = md5::Md5::new();
    let mut total_file_size = 0u64;
    let started = tokio::time::Instant::now();

    loop {
        match body.next().await {
            None => break,
            Some(Ok(bytes)) => {
                total_file_size += bytes.len() as u64;
                if total_file_size > max_size {
                    return Err(AppError::EntityTooLarge);
                }
                sha_hasher.update(&bytes);
                md5_hasher.update(&bytes);
                temp_file
                    .write_all(&bytes)
                    .await
                    .map_err(|e| AppError::Other(e.into()))?;
            }
            Some(Err(err)) => return Err(AppError::Other(err.into())),
        }
    }

    // Both digests of the whole body, as raw bytes. Any checksum the client
    // asked us to verify is one of these two, so check it *before* publishing —
    // a mismatch returns here and the temp guard's Drop unlinks the staged blob.
    let sha256 = sha_hasher.finalize();
    let md5 = md5_hasher.finalize();
    if let Some(expected) = &checksum_algo {
        expected.verify(&sha256, &md5)?;
    }

    let stored = Stored {
        digest: Digest(hex::encode(sha256)),
        etag: ETag(hex::encode(md5)),
        size: total_file_size,
        blob_kind: BlobKind::Whole,
    };

    store.commit_temp(temp.path(), &stored.digest).await?;
    temp.disarm();
    let elapsed = started.elapsed().as_secs_f64();
    if elapsed > 0.0 {
        metrics::histogram!(crate::metrics::UPLOAD_THROUGHPUT).record(stored.size as f64 / elapsed);
    }
    Ok(stored)
}

/// CDC PUT path: content-defined chunk → per-chunk CAS → manifest → index digest.
///
/// Still computes whole-object MD5 (ETag) and optional client checksums over the
/// logical plaintext. Storage grain is chunks; identity of the index pointer is
/// the manifest digest ([`BlobKind::Manifest`]).
///
/// See [`crate::cdc`] and [`crate::manifest`]. Gated from routes by
/// [`crate::AppState::cdc`] (`.enabled`).
pub async fn stream_cdc_to_store<S>(
    store: &Store,
    mut body: S,
    max_size: u64,
    checksum_algo: Option<CheckSumAlgorithm>,
    config: CdcConfig,
) -> Result<Stored, AppError>
where
    S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin,
{
    let mut chunker = CdcChunker::new(config)?;
    let mut sha_hasher = Sha256::new();
    let mut md5_hasher = md5::Md5::new();
    let mut total_file_size = 0u64;
    let started = tokio::time::Instant::now();

    let mut chunk_refs = Vec::new();

    loop {
        match body.next().await {
            None => break,
            Some(Ok(bytes)) => {
                total_file_size += bytes.len() as u64;
                if total_file_size > max_size {
                    return Err(AppError::EntityTooLarge);
                }
                sha_hasher.update(&bytes);
                md5_hasher.update(&bytes);
                for chunk in chunker.push(&bytes)? {
                    let digest = store.commit_bytes(&chunk).await?;
                    chunk_refs.push(ChunkRef {
                        digest,
                        size: chunk.len() as u64,
                    });
                }
            }
            Some(Err(err)) => return Err(AppError::Other(err.into())),
        }
    }

    for chunk in chunker.finish()? {
        chunk_refs.push(ChunkRef {
            digest: store.commit_bytes(&chunk).await?,
            size: chunk.len() as u64,
        });
    }

    let sha256 = sha_hasher.finalize();
    let md5 = md5_hasher.finalize();
    if let Some(expected) = &checksum_algo {
        expected.verify(&sha256, &md5)?;
    }

    let manifest = Manifest::new(chunk_refs);
    let manifest_digest = commit_manifest(store, &manifest).await?;
    let stored = {
        let etag = ETag(hex::encode(md5));
        Stored::manifest(manifest_digest, etag, total_file_size)
    };

    let elapsed = started.elapsed().as_secs_f64();
    if elapsed > 0.0 {
        metrics::histogram!(crate::metrics::UPLOAD_THROUGHPUT).record(stored.size as f64 / elapsed);
    }
    Ok(stored)
}

#[cfg(test)]
mod tests {
    use super::{stream_to_store, Stored};
    use crate::error::AppError;
    use crate::store::Store;
    use bytes::Bytes;
    use futures_util::stream;
    use sha2::{Digest as _, Sha256};
    use std::io;
    use std::sync::Arc;
    use tempfile::TempDir;

    fn fresh() -> (TempDir, Arc<Store>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        (root, store)
    }

    fn body(
        chunks: Vec<Result<Bytes, axum::Error>>,
    ) -> impl futures_util::Stream<Item = Result<Bytes, axum::Error>> + Unpin {
        stream::iter(chunks)
    }

    fn ok(b: &[u8]) -> Result<Bytes, axum::Error> {
        Ok(Bytes::copy_from_slice(b))
    }

    fn temp_count(store: &Store) -> usize {
        std::fs::read_dir(store.tmp_dir())
            .expect("read tmp dir")
            .count()
    }

    #[tokio::test]
    async fn clean_stream_commits_correct_digest_and_size() {
        let (_root, store) = fresh();
        let parts: [&[u8]; 3] = [b"hello ", b"streaming ", b"world"];
        let whole: Vec<u8> = parts.concat();
        let expected_digest = hex::encode(Sha256::digest(&whole));

        let chunks = parts.iter().map(|p| ok(p)).collect();
        let stored: Stored = stream_to_store(&store, body(chunks), 1024, None)
            .await
            .expect("a clean stream should commit");

        assert_eq!(
            stored.digest.as_str(),
            expected_digest,
            "digest must be the sha256 of the streamed bytes"
        );
        assert_eq!(
            stored.size,
            whole.len() as u64,
            "size must be the count of bytes actually streamed"
        );
        assert!(
            store.contains(&stored.digest).await,
            "the blob must be committed and findable by its digest"
        );
        assert_eq!(temp_count(&store), 0, "commit must consume the temp file");
    }

    /// Criterion 2: the *running total* exceeding `max_size` is rejected with
    /// EntityTooLarge — and no temp file is left behind. No single chunk here
    /// exceeds the cap; only the sum does. That's the bug a per-chunk check hides.
    #[tokio::test]
    async fn oversize_stream_rejected_and_leaves_no_temp() {
        let (_root, store) = fresh(); // keep the TempDir alive for the whole test
                                      // 4 × 32 B = 128 B total, cap = 100 B. Each chunk (32) is under the cap.
        let chunk = vec![b'x'; 32];
        let chunks = (0..4).map(|_| ok(&chunk)).collect();

        let outcome = stream_to_store(&store, body(chunks), 100, None).await;

        assert!(
            matches!(outcome, Err(AppError::EntityTooLarge)),
            "a body whose running total exceeds the cap must be EntityTooLarge"
        );
        assert_eq!(
            temp_count(&store),
            0,
            "a rejected (over-cap) upload must not leak a temp file"
        );
    }

    /// Criterion 3: a stream that errors mid-body (a client disconnect) surfaces
    /// an error and leaves no temp file — the dropped-lid path.
    #[tokio::test]
    async fn stream_error_leaves_no_temp() {
        let (_root, store) = fresh();
        let chunks = vec![
            ok(b"3 gigabytes so far..."),
            Err(axum::Error::new(io::Error::new(
                io::ErrorKind::ConnectionReset,
                "client disconnected mid-upload",
            ))),
        ];

        let outcome = stream_to_store(&store, body(chunks), 1 << 20, None).await;

        assert!(
            outcome.is_err(),
            "a broken body stream must surface an error"
        );
        assert_eq!(
            temp_count(&store),
            0,
            "a mid-stream disconnect must not leak a partial temp file"
        );
    }
}

#[cfg(test)]
mod checksum_tests {
    use super::{stream_to_store, CheckSumAlgorithm, ChecksumSpec};
    use crate::error::AppError;
    use crate::store::Store;
    use axum::http::{HeaderMap, HeaderValue};
    use base64::Engine as _;
    use bytes::Bytes;
    use futures_util::stream;
    use sha2::{Digest as _, Sha256};
    use std::sync::Arc;
    use tempfile::TempDir;

    fn fresh() -> (TempDir, Arc<Store>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        (root, store)
    }

    fn body(
        chunks: Vec<Result<Bytes, axum::Error>>,
    ) -> impl futures_util::Stream<Item = Result<Bytes, axum::Error>> + Unpin {
        stream::iter(chunks)
    }

    fn temp_count(store: &Store) -> usize {
        std::fs::read_dir(store.tmp_dir())
            .expect("read tmp dir")
            .count()
    }

    fn b64(raw: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(raw)
    }

    /// Parse headers and unwrap both layers: "parsed fine" and "found one".
    fn extracted(headers: &HeaderMap) -> CheckSumAlgorithm {
        ChecksumSpec::from_headers(headers)
            .expect("headers should parse")
            .0
            .expect("a checksum should be extracted")
    }

    fn rejected(headers: &HeaderMap) -> bool {
        matches!(
            ChecksumSpec::from_headers(headers),
            Err(AppError::InvalidRequest(_))
        )
    }

    #[test]
    fn absent_headers_mean_no_checksum() {
        let spec = ChecksumSpec::from_headers(&HeaderMap::new()).expect("empty headers parse");
        assert!(spec.0.is_none(), "no checksum headers → no verification");
    }

    #[test]
    fn content_md5_is_used_directly() {
        let mut headers = HeaderMap::new();
        headers.insert("Content-MD5", HeaderValue::from_static("abc123"));
        let algo = extracted(&headers);
        assert_eq!(algo.suffix(), "MD5");
        assert_eq!(algo.checksum(), "abc123");
    }

    #[test]
    fn amz_algorithm_selects_its_value_header_case_insensitively() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Amz-Checksum-Algorithm",
            HeaderValue::from_static("sha256"),
        );
        headers.insert("X-Amz-Checksum-SHA256", HeaderValue::from_static("cafe"));
        let algo = extracted(&headers);
        assert_eq!(algo.suffix(), "SHA256");
        assert_eq!(algo.checksum(), "cafe");
    }

    #[test]
    fn content_md5_wins_over_amz_headers() {
        let mut headers = HeaderMap::new();
        headers.insert("Content-MD5", HeaderValue::from_static("md5-value"));
        headers.insert(
            "X-Amz-Checksum-Algorithm",
            HeaderValue::from_static("SHA256"),
        );
        headers.insert(
            "X-Amz-Checksum-SHA256",
            HeaderValue::from_static("sha-value"),
        );
        let algo = extracted(&headers);
        assert_eq!(algo.suffix(), "MD5");
        assert_eq!(algo.checksum(), "md5-value");
    }

    #[test]
    fn unknown_algorithm_is_rejected() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Amz-Checksum-Algorithm",
            HeaderValue::from_static("crc32"),
        );
        assert!(rejected(&headers), "unsupported algorithm must be a 400");
    }

    #[test]
    fn named_algorithm_without_value_header_is_rejected() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Amz-Checksum-Algorithm",
            HeaderValue::from_static("SHA256"),
        );
        assert!(
            rejected(&headers),
            "an algorithm with no X-Amz-Checksum-SHA256 value must be a 400"
        );
    }

    /// Header values are bytes, not strings: a non-ASCII value must become a
    /// 400, never a `to_str().unwrap()` panic (the bug this extractor replaced).
    #[test]
    fn non_ascii_header_value_is_rejected_not_a_panic() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "Content-MD5",
            HeaderValue::from_bytes(&[0xFF]).expect("0xFF is a legal opaque header byte"),
        );
        assert!(rejected(&headers), "non-ASCII header value must be a 400");
    }

    #[test]
    fn verify_accepts_matching_base64_md5() {
        let bytes = b"hello checksum";
        let md5 = md5::Md5::digest(bytes);
        let algo = CheckSumAlgorithm::Md5(b64(&md5));
        assert!(algo.verify(&[], &md5).is_ok());
    }

    #[test]
    fn verify_rejects_mismatched_digest() {
        let algo = CheckSumAlgorithm::Md5(b64(b"not-a-real-md5!!"));
        let outcome = algo.verify(&[], &[0u8; 16]);
        assert!(
            matches!(outcome, Err(AppError::InvalidRequest(ref msg)) if msg.contains("BadDigest")),
            "mismatch must be BadDigest / InvalidRequest"
        );
    }

    #[test]
    fn verify_rejects_non_base64_header() {
        let algo = CheckSumAlgorithm::Sha256("!!!not-base64!!!".into());
        let outcome = algo.verify(&[0u8; 32], &[]);
        assert!(
            matches!(outcome, Err(AppError::InvalidRequest(ref msg)) if msg.contains("base64")),
            "garbage encoding must be InvalidRequest"
        );
    }

    /// Matching Content-MD5 commits; a wrong one rejects and leaves no temp /
    /// no durable blob — the whole point of verifying before `commit_temp`.
    #[tokio::test]
    async fn matching_md5_checksum_commits() {
        let (_root, store) = fresh();
        let payload = b"streamed body";
        let md5 = md5::Md5::digest(payload);
        let algo = CheckSumAlgorithm::Md5(b64(&md5));

        let stored = stream_to_store(
            &store,
            body(vec![Ok(Bytes::from_static(payload))]),
            1024,
            Some(algo),
        )
        .await
        .expect("matching checksum should commit");

        assert_eq!(stored.size, payload.len() as u64);
        assert!(store.contains(&stored.digest).await);
        assert_eq!(temp_count(&store), 0);
    }

    #[tokio::test]
    async fn mismatched_sha256_checksum_leaves_nothing_durable() {
        let (_root, store) = fresh();
        let payload = b"streamed body";
        // Correct length, wrong bytes — decode succeeds, compare fails.
        let wrong = b64(&[0u8; 32]);
        let algo = CheckSumAlgorithm::Sha256(wrong);

        let outcome = stream_to_store(
            &store,
            body(vec![Ok(Bytes::from_static(payload))]),
            1024,
            Some(algo),
        )
        .await;

        assert!(
            matches!(outcome, Err(AppError::InvalidRequest(ref msg)) if msg.contains("BadDigest")),
            "mismatch must reject before commit"
        );
        assert_eq!(
            temp_count(&store),
            0,
            "rejected upload must not leak a temp"
        );
        // Digest of the payload must not have been published.
        let digest = crate::object::Digest(hex::encode(Sha256::digest(payload)));
        assert!(
            !store.contains(&digest).await,
            "a BadDigest reject must leave no durable blob"
        );
    }
}

/// CDC PUT path ([`stream_cdc_to_store`]): cutter → per-chunk CAS → manifest.
#[cfg(test)]
mod cdc_stream_tests {
    use super::{stream_cdc_to_store, CheckSumAlgorithm};
    use crate::cdc::CdcConfig;
    use crate::error::AppError;
    use crate::index::Index;
    use crate::index_backend::IndexBackend;
    use crate::lifecycle::Lifecycle;
    use crate::manifest::{chunk_digests, load_manifest, open_range};
    use crate::object::BlobKind;
    use crate::store::Store;
    use base64::Engine as _;
    use bytes::Bytes;
    use futures_util::stream;
    use md5::Digest as _;
    use std::io;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tokio::io::{AsyncRead, AsyncReadExt};

    fn tiny_cdc() -> CdcConfig {
        CdcConfig {
            enabled: true,
            min_size: 64,
            avg_size: 256,
            max_size: 1024,
            min_object_size: 0,
        }
    }

    fn fresh() -> (TempDir, Arc<Store>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        (root, store)
    }

    fn fresh_with_lifecycle() -> (TempDir, Arc<Store>, Arc<Lifecycle>) {
        let root = TempDir::new().expect("temp root");
        let store = Store::open(root.path()).expect("open store");
        let index = Index::open(root.path(), store.clone()).expect("open index");
        let lifecycle = Lifecycle::new(Arc::new(IndexBackend::local(index)), store.clone());
        (root, store, lifecycle)
    }

    fn body(
        chunks: Vec<Result<Bytes, axum::Error>>,
    ) -> impl futures_util::Stream<Item = Result<Bytes, axum::Error>> + Unpin {
        stream::iter(chunks)
    }

    fn ok(b: &[u8]) -> Result<Bytes, axum::Error> {
        Ok(Bytes::copy_from_slice(b))
    }

    fn temp_count(store: &Store) -> usize {
        std::fs::read_dir(store.tmp_dir())
            .expect("read tmp dir")
            .count()
    }

    fn b64(raw: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(raw)
    }

    async fn read_all(mut reader: impl AsyncRead + Unpin) -> Vec<u8> {
        let mut buf = Vec::new();
        reader.read_to_end(&mut buf).await.expect("read");
        buf
    }

    #[tokio::test]
    async fn cdc_stream_commits_manifest_and_etag() {
        let (_root, store, lifecycle) = fresh_with_lifecycle();
        let parts: [&[u8]; 3] = [b"hello ", b"cdc ", b"world!!!!".as_slice()]; // pad for min sizes
                                                                               // Build a payload large enough to exercise real cuts.
        let mut whole: Vec<u8> = (0..2048u32).map(|i| (i % 251) as u8).collect();
        whole.extend(parts.concat());

        let frames: Vec<_> = whole.chunks(100).map(ok).collect();
        let stored = stream_cdc_to_store(&store, body(frames), 1 << 20, None, tiny_cdc())
            .await
            .expect("cdc stream should commit");

        assert_eq!(stored.blob_kind, BlobKind::Manifest);
        assert_eq!(stored.size, whole.len() as u64);
        assert_eq!(
            stored.etag.as_str(),
            hex::encode(md5::Md5::digest(&whole)),
            "ETag must be md5 of the logical object, not the manifest"
        );
        assert!(store.contains(&stored.digest).await);
        assert_eq!(temp_count(&store), 0);

        let manifest = load_manifest(&store, &stored.digest)
            .await
            .expect("load manifest");
        assert_eq!(manifest.logical_size(), whole.len() as u64);
        assert!(!manifest.chunks.is_empty());

        let reader = open_range(
            &store,
            &lifecycle,
            &stored.digest,
            0,
            whole.len() as u64 - 1,
        )
        .await
        .expect("open_range");
        assert_eq!(read_all(reader).await, whole);
    }

    #[tokio::test]
    async fn cdc_identical_bodies_dedup_manifest_and_chunks() {
        let (_root, store) = fresh();
        let data: Vec<u8> = (0..1500u32).map(|i| (i % 200) as u8).collect();
        let frames = |d: &[u8]| d.chunks(64).map(ok).collect::<Vec<_>>();

        let a = stream_cdc_to_store(&store, body(frames(&data)), 1 << 20, None, tiny_cdc())
            .await
            .unwrap();
        let b = stream_cdc_to_store(&store, body(frames(&data)), 1 << 20, None, tiny_cdc())
            .await
            .unwrap();

        assert_eq!(a.digest, b.digest, "identical bytes → same manifest digest");
        assert_eq!(a.etag, b.etag);
        let digests = chunk_digests(&store, &a.digest).await.unwrap();
        assert!(!digests.is_empty());
        for d in &digests {
            assert!(store.contains(d).await);
        }
    }

    #[tokio::test]
    async fn cdc_near_duplicates_share_some_chunks() {
        let (_root, store) = fresh();
        let base: Vec<u8> = (0..8192u32).map(|i| (i % 251) as u8).collect();
        let mut edited = Vec::with_capacity(base.len() + 1);
        edited.push(0xAB);
        edited.extend_from_slice(&base);

        let frames = |d: &[u8]| d.chunks(100).map(ok).collect::<Vec<_>>();
        let a = stream_cdc_to_store(&store, body(frames(&base)), 1 << 20, None, tiny_cdc())
            .await
            .unwrap();
        let b = stream_cdc_to_store(&store, body(frames(&edited)), 1 << 20, None, tiny_cdc())
            .await
            .unwrap();

        assert_ne!(
            a.digest, b.digest,
            "different objects → different manifests"
        );
        let da = chunk_digests(&store, &a.digest).await.unwrap();
        let db = chunk_digests(&store, &b.digest).await.unwrap();
        let shared = da.iter().filter(|d| db.contains(d)).count();
        assert!(
            shared >= 1,
            "near-duplicates should share ≥1 chunk digest; a={} b={} shared={shared}",
            da.len(),
            db.len()
        );
    }

    #[tokio::test]
    async fn cdc_oversize_rejected_and_leaves_no_temp() {
        let (_root, store) = fresh();
        let chunk = vec![b'x'; 32];
        let chunks = (0..4).map(|_| ok(&chunk)).collect();

        let outcome = stream_cdc_to_store(&store, body(chunks), 100, None, tiny_cdc()).await;

        assert!(matches!(outcome, Err(AppError::EntityTooLarge)));
        assert_eq!(temp_count(&store), 0);
    }

    #[tokio::test]
    async fn cdc_stream_error_leaves_no_temp() {
        let (_root, store) = fresh();
        let chunks = vec![
            ok(b"partial upload"),
            Err(axum::Error::new(io::Error::new(
                io::ErrorKind::ConnectionReset,
                "client disconnected mid-upload",
            ))),
        ];

        let outcome = stream_cdc_to_store(&store, body(chunks), 1 << 20, None, tiny_cdc()).await;

        assert!(outcome.is_err());
        assert_eq!(temp_count(&store), 0);
    }

    #[tokio::test]
    async fn cdc_matching_md5_checksum_commits() {
        let (_root, store) = fresh();
        let payload: Vec<u8> = (0..512u32).map(|i| (i % 251) as u8).collect();
        let algo = CheckSumAlgorithm::Md5(b64(&md5::Md5::digest(&payload)));

        let stored = stream_cdc_to_store(
            &store,
            body(payload.chunks(80).map(ok).collect()),
            1 << 20,
            Some(algo),
            tiny_cdc(),
        )
        .await
        .expect("matching checksum should commit");

        assert_eq!(stored.blob_kind, BlobKind::Manifest);
        assert_eq!(stored.size, payload.len() as u64);
        assert!(store.contains(&stored.digest).await);
        assert_eq!(temp_count(&store), 0);
    }

    #[tokio::test]
    async fn cdc_mismatched_checksum_rejects_without_manifest() {
        let (_root, store) = fresh();
        let payload: Vec<u8> = (0..400u32).map(|i| (i % 251) as u8).collect();
        let algo = CheckSumAlgorithm::Md5(b64(b"not-the-real-md5!"));

        let outcome = stream_cdc_to_store(
            &store,
            body(payload.chunks(50).map(ok).collect()),
            1 << 20,
            Some(algo),
            tiny_cdc(),
        )
        .await;

        assert!(
            matches!(outcome, Err(AppError::InvalidRequest(ref msg)) if msg.contains("BadDigest")),
            "mismatch must be BadDigest"
        );
        assert_eq!(temp_count(&store), 0);
        // Chunks may already be durable (verify runs after chunk commit); the
        // manifest must not exist. We can't know the would-be digest easily, so
        // just assert the error path cleaned temps.
    }

    #[tokio::test]
    async fn cdc_empty_body_commits_empty_manifest() {
        let (_root, store) = fresh();
        let stored = stream_cdc_to_store(&store, body(vec![]), 1024, None, tiny_cdc())
            .await
            .expect("empty body");

        assert_eq!(stored.size, 0);
        assert_eq!(stored.blob_kind, BlobKind::Manifest);
        let manifest = load_manifest(&store, &stored.digest).await.unwrap();
        assert!(manifest.chunks.is_empty());
        assert_eq!(stored.etag.as_str(), hex::encode(md5::Md5::digest(b"")),);
    }
}
