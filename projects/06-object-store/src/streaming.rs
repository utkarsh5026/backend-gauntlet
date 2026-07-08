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
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::AsyncWriteExt;

pub struct Stored {
    pub digest: Digest,
    pub etag: ETag,
    pub size: u64,
}

pub async fn stream_to_store<S>(
    store: &Store,
    mut body: S,
    max_size: u64,
) -> Result<Stored, AppError>
where
    S: futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin,
{
    use tokio::fs as tfs;
    let temp_dir = store.tmp_dir();
    let temp_path = temp_dir.join(format!(
        "upload-{:x}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time before UNIX epoch")
            .as_nanos()
    ));
    let mut temp_file = tokio::fs::File::create(&temp_path).await?;
    let mut sha_hasher = Sha256::new();
    let mut md5_hasher = md5::Md5::new();
    let mut total_file_size = 0u64;

    loop {
        match body.next().await {
            None => break,
            Some(Ok(bytes)) => {
                total_file_size += bytes.len() as u64;
                if total_file_size > max_size {
                    tfs::remove_file(&temp_path).await?;
                    return Err(AppError::EntityTooLarge);
                }
                sha_hasher.update(&bytes);
                md5_hasher.update(&bytes);
                if let Err(e) = temp_file.write_all(&bytes).await {
                    tfs::remove_file(&temp_path).await?;
                    return Err(AppError::Other(e.into()));
                }
            }
            Some(Err(err)) => {
                tfs::remove_file(&temp_path).await?;
                return Err(AppError::Other(err.into()));
            }
        }
    }

    let stored = {
        let digest = Digest(hex::encode(sha_hasher.finalize()));
        let etag = ETag(hex::encode(md5_hasher.finalize()));
        let size = total_file_size;
        Stored { digest, etag, size }
    };

    store.commit_temp(&temp_path, &stored.digest).await?;
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
