//! V2 — Streaming bodies, end to end: bounded memory + backpressure.
//!
//! This is where "10 KB on a laptop" and "5 GB in prod" stop being the same
//! program. The request body is pulled one chunk at a time, written straight to
//! a temp file and fed to the hashers — so an object of *any* size costs O(1)
//! memory. Collecting the body into a `Vec<u8>` is the single bug this whole
//! vertical exists to prevent.

use crate::error::AppError;
use crate::object::{Digest, ETag};
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
    /// Returns [`AppError::InvalidRequest`] when the computed digest doesn't match
    /// what the client sent (S3 calls this `BadDigest`, a 400).
    fn verify(&self, sha256: &[u8], md5: &[u8]) -> Result<(), AppError> {
        // Pick the raw digest for the algorithm the client asked about — the
        // whole point of reusing the two body hashers instead of a third.
        let computed = match self {
            CheckSumAlgorithm::Sha256(_) => sha256,
            CheckSumAlgorithm::Md5(_) => md5,
        };
        let _ = computed;
        // TODO(V-checksum): decode `self.checksum()` and compare it against
        // `computed`. Mind the encoding — S3 sends the header as base64 of the
        // raw digest, so decode it rather than comparing against the hex
        // `Digest`/`ETag`; return AppError::InvalidRequest on mismatch.
        todo!("compare the client checksum against the matching computed digest")
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
/// Produced by [`stream_to_store`] once the staged temp file is committed. The
/// caller (V3's PUT handler) records these on the `(bucket, key)` index row and
/// echoes the `ETag` and size back to the client.
pub struct Stored {
    /// SHA-256 of the streamed bytes, hex-encoded — the blob's content address
    /// (and its name on disk) in the [`Store`].
    pub digest: Digest,
    /// The single-PUT S3 `ETag`, `hex(md5(bytes))`. See [`ETag`] for why this is
    /// deliberately *not* the same value as the content [`Digest`].
    pub etag: ETag,
    /// Total number of bytes streamed — the object's size.
    pub size: u64,
}

/// Stream a request body to disk chunk by chunk, hashing as it goes, and commit
/// it as a content-addressed blob.
///
/// This is the whole point of V2: the body is pulled one [`Bytes`](bytes::Bytes)
/// chunk at a time, written straight to a temp file and fed to the SHA-256
/// (content digest) and MD5 (`ETag`) hashers, so memory stays O(1) regardless of
/// object size — never collect the body into a `Vec<u8>`. Awaiting each file
/// write is also the backpressure: a fast producer is throttled to disk speed.
/// The staged temp file is owned by a [`TempEntry`](crate::store::TempEntry)
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
    let mut temp = store.tmp_file("stream");
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
    };

    store.commit_temp(temp.path(), &stored.digest).await?;
    temp.disarm();
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
    use super::{CheckSumAlgorithm, ChecksumSpec};
    use crate::error::AppError;
    use axum::http::{HeaderMap, HeaderValue};

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
}
