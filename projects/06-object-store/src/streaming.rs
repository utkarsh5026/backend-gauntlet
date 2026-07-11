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
use futures_util::StreamExt;
use sha2::{Digest as _, Sha256};
use tokio::io::AsyncWriteExt;

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
/// let stored = rt.block_on(stream_to_store(&store, body, 1024)).unwrap();
/// assert_eq!(stored.size, 11);
/// ```
pub async fn stream_to_store<S>(
    store: &Store,
    mut body: S,
    max_size: u64,
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

    let stored = {
        let digest = Digest(hex::encode(sha_hasher.finalize()));
        let etag = ETag(hex::encode(md5_hasher.finalize()));
        let size = total_file_size;
        Stored { digest, etag, size }
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
        let stored: Stored = stream_to_store(&store, body(chunks), 1024)
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

        let outcome = stream_to_store(&store, body(chunks), 100).await;

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

        let outcome = stream_to_store(&store, body(chunks), 1 << 20).await;

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
