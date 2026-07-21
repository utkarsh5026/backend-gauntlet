//! Chunk manifests — the recipe that turns CDC chunks back into one object.
//!
//! With chunk-level dedup, the index does **not** point at the object's raw
//! bytes. It points at a small content-addressed **manifest** blob:
//!
//! ```text
//! index:  (bucket, key) → manifest digest M   (BlobKind::Manifest)
//! store:  objects/<M>   = ordered [ChunkRef…]
//!         objects/<d_i> = plaintext chunk bytes
//! ```
//!
//! The client still sees one object (ETag / size / Range over *logical* bytes).
//! Assembly is internal. See
//! [`docs/10-how-chunk-level-dedup-works.md`](../docs/10-how-chunk-level-dedup-works.md).
//!
//! ## Invariants
//!
//! - Each [`ChunkRef::digest`] is the SHA-256 of that chunk's **plaintext**
//!   (hash-then-compress — same identity rule as the cold tier).
//! - `sum(chunk.size) ==` the live version's logical `size`.
//! - GC must mark the manifest **and** every chunk it names, or shared chunks
//!   get reaped while another key still needs them.
//! - Blob-then-pointer: all chunks + the manifest must be durable before the
//!   index flip ([`crate::index::Index::put`]).

use md5::Digest as _;
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::collections::VecDeque;
use std::io;
use std::pin::Pin;
use std::task::{Context, Poll};

use async_compression::tokio::bufread::ZstdDecoder;
use tokio::io::{AsyncRead, AsyncReadExt, BufReader, ReadBuf};

use crate::error::AppError;
use crate::lifecycle::{Encoding, Lifecycle};
use crate::object::Digest;
use crate::store::Store;

/// One slice in a CDC-assembled object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkRef {
    /// Plaintext SHA-256 of the chunk bytes (CAS name under `objects/`).
    pub digest: Digest,
    /// Plaintext length of this chunk.
    pub size: u64,
}

impl ChunkRef {
    /// Hash plaintext chunk bytes into a CAS pointer (`digest` + `size`).
    pub fn from_bytes(chunk: &[u8]) -> Self {
        Self {
            digest: Digest(hex::encode(Sha256::digest(chunk))),
            size: chunk.len() as u64,
        }
    }
}

/// Ordered recipe for one logical object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    /// Schema / format version for on-disk JSON (start at 1).
    pub version: u32,
    pub chunks: Vec<ChunkRef>,
}

impl Manifest {
    pub const FORMAT_VERSION: u32 = 1;

    pub fn new(chunks: Vec<ChunkRef>) -> Self {
        Self {
            version: Self::FORMAT_VERSION,
            chunks,
        }
    }

    /// Logical object size = sum of chunk sizes.
    pub fn logical_size(&self) -> u64 {
        self.chunks.iter().map(|c| c.size).sum()
    }

    /// Serialize to the bytes that will be stored as the manifest CAS blob.
    pub fn to_bytes(&self) -> Result<Vec<u8>, AppError> {
        Ok(serde_json::to_vec(self)?)
    }

    /// Parse bytes loaded from the CAS.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, AppError> {
        Ok(serde_json::from_slice(bytes)?)
    }
}

/// Commit `manifest` as its own content-addressed blob; return its digest.
///
/// With CDC, the index pointer is **not** the object's payload digest — it is
/// the digest of this small recipe blob ([`crate::object::BlobKind::Manifest`]).
/// Chunk plaintext must already be durable under each [`ChunkRef::digest`]
/// before this runs (blob-then-pointer).
///
/// ## What to hash
///
/// Hash the **manifest bytes** from [`Manifest::to_bytes`] (the JSON recipe),
/// not the logical object payload. Callers still compute whole-object MD5 /
/// SHA separately for the S3 `ETag` and optional checksum headers.
///
/// ## Publish steps
///
/// 1. Serialize with [`Manifest::to_bytes`].
/// 2. [`Store::commit_bytes`] — SHA-256 → stage under `tmp/` →
///    [`Store::commit_temp`] (dedup / metrics / scrubber wake).
///
/// ## Errors
///
/// Propagates serialization failures and any I/O / publish error from the
/// temp → rename dance.
pub async fn commit_manifest(store: &Store, manifest: &Manifest) -> Result<Digest, AppError> {
    let bytes = manifest.to_bytes()?;
    store.commit_bytes(&bytes).await
}

/// Load and parse the manifest stored under `digest`.
pub async fn load_manifest(store: &Store, digest: &Digest) -> Result<Manifest, AppError> {
    let mut file = store.open_blob(digest).await?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).await?;
    Manifest::from_bytes(&bytes)
}

/// Chunk digests named by the manifest at `digest` (for GC mark expansion).
pub async fn chunk_digests(store: &Store, digest: &Digest) -> Result<Vec<Digest>, AppError> {
    let manifest = load_manifest(store, digest).await?;
    Ok(manifest.chunks.into_iter().map(|c| c.digest).collect())
}

/// Map a logical byte range `[start, end]` (inclusive) onto chunk file slices.
///
/// Used by ranged GET: skip whole chunks before `start`, then stream a prefix /
/// middle / suffix of the overlapping chunks.
pub fn map_range(manifest: &Manifest, start: u64, end: u64) -> Result<Vec<ChunkSlice>, AppError> {
    // Chunk spans are half-open `[chunk_start, chunk_end)`; convert inclusive
    // `end` to an exclusive bound so overlap math stays consistent.
    let exclusive_end = end.saturating_add(1);
    let mut offset = 0u64;
    let mut slices = Vec::new();
    for chunk in &manifest.chunks {
        if offset >= exclusive_end {
            break;
        }
        let chunk_start = offset;
        let chunk_end = chunk_start + chunk.size;
        if chunk_end <= start {
            offset = chunk_end;
            continue;
        }
        let slice_start = start.max(chunk_start);
        let slice_end = exclusive_end.min(chunk_end);
        slices.push(ChunkSlice {
            digest: chunk.digest.clone(),
            offset: slice_start - chunk_start,
            len: slice_end - slice_start,
        });
        offset = chunk_end;
    }
    Ok(slices)
}

/// A slice of one chunk file that contributes to a logical Range response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChunkSlice {
    /// Plaintext SHA-256 of the chunk bytes (CAS name under `objects/`).
    pub digest: Digest,
    /// Offset within the chunk's plaintext bytes.
    pub offset: u64,
    /// Number of bytes to read from that offset.
    pub len: u64,
}

/// Stream logical bytes `[start, end]` by reading chunk blobs in order.
///
/// Must honor cold-tier encoding per chunk via
/// [`crate::lifecycle::Lifecycle::locate`] — same as today's whole-object GET,
/// but once per chunk. Returns a concrete [`ManifestRangeReader`] (no `dyn`).
pub async fn open_range(
    store: &Store,
    lifecycle: &Lifecycle,
    manifest_digest: &Digest,
    start: u64,
    end: u64,
) -> Result<ManifestRangeReader, AppError> {
    let manifest = load_manifest(store, manifest_digest).await?;
    let slices = map_range(&manifest, start, end)?;

    let mut parts = VecDeque::with_capacity(slices.len());
    for slice in slices {
        if slice.len == 0 {
            continue;
        }
        parts.push_back(open_chunk_slice(store, lifecycle, &slice).await?);
    }
    Ok(ManifestRangeReader { parts })
}

/// Concatenates ordered [`ChunkSliceReader`]s into one logical byte stream.
#[derive(Debug)]
pub struct ManifestRangeReader {
    parts: VecDeque<ChunkSliceReader>,
}

impl AsyncRead for ManifestRangeReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.as_mut().get_mut();
        loop {
            let Some(front) = this.parts.front_mut() else {
                return Poll::Ready(Ok(()));
            };
            let filled_before = buf.filled().len();
            match Pin::new(front).poll_read(cx, buf) {
                Poll::Ready(Ok(())) if buf.filled().len() == filled_before => {
                    this.parts.pop_front();
                }
                other => return other,
            }
        }
    }
}

/// One opened chunk slice — hot seek or cold zstd decode.
#[derive(Debug)]
enum ChunkSliceReader {
    Hot(tokio::io::Take<tokio::fs::File>),
    Cold(tokio::io::Take<ZstdDecoder<BufReader<tokio::fs::File>>>),
}

impl AsyncRead for ChunkSliceReader {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Hot(r) => Pin::new(r).poll_read(cx, buf),
            Self::Cold(r) => Pin::new(r).poll_read(cx, buf),
        }
    }
}

/// Open one [`ChunkSlice`]: seek on hot raw blobs, skip+take on cold zstd.
async fn open_chunk_slice(
    store: &Store,
    lifecycle: &Lifecycle,
    slice: &ChunkSlice,
) -> Result<ChunkSliceReader, AppError> {
    let physical = lifecycle.locate(&slice.digest).await?;
    match physical.encoding {
        Encoding::Raw => {
            let inclusive_end = slice.offset + slice.len - 1;
            let file = store
                .open_blob_range(&slice.digest, slice.offset, inclusive_end)
                .await?;
            Ok(ChunkSliceReader::Hot(file))
        }
        Encoding::Zstd => {
            let file = tokio::fs::File::open(&physical.path).await?;
            let mut decoder = ZstdDecoder::new(BufReader::new(file));
            if slice.offset > 0 {
                let mut skip = (&mut decoder).take(slice.offset);
                tokio::io::copy(&mut skip, &mut tokio::io::sink()).await?;
            }
            Ok(ChunkSliceReader::Cold(decoder.take(slice.len)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::durable::TempEntry;
    use crate::index::Index;
    use crate::index_backend::IndexBackend;
    use std::sync::Arc;
    use tempfile::TempDir;

    fn ref_at(label: &str, size: u64) -> ChunkRef {
        ChunkRef {
            // Labels are not real digests — map_range only needs identity + size.
            digest: Digest(label.into()),
            size,
        }
    }

    fn two_chunks() -> Manifest {
        Manifest::new(vec![ref_at("aa", 10), ref_at("bb", 10)])
    }

    fn setup(root: &std::path::Path) -> (Arc<Lifecycle>, Arc<Store>) {
        let store = Store::open(root).expect("open store");
        let index = Index::open(root, store.clone()).expect("open index");
        let lifecycle = Lifecycle::new(Arc::new(IndexBackend::local(index)), store.clone());
        (lifecycle, store)
    }

    async fn commit_chunk(store: &Store, bytes: &[u8]) -> ChunkRef {
        let chunk_ref = ChunkRef::from_bytes(bytes);
        let mut temp = TempEntry::unique_in(store.tmp_dir(), "test-chunk");
        tokio::fs::write(temp.path(), bytes)
            .await
            .expect("write chunk");
        store
            .commit_temp(temp.path(), &chunk_ref.digest)
            .await
            .expect("commit chunk");
        temp.disarm();
        chunk_ref
    }

    async fn read_all(mut reader: ManifestRangeReader) -> Vec<u8> {
        let mut buf = Vec::new();
        reader.read_to_end(&mut buf).await.expect("read");
        buf
    }

    #[test]
    fn logical_size_sums_chunk_sizes() {
        let m = Manifest::new(vec![ref_at("aa", 10), ref_at("bb", 25)]);
        assert_eq!(m.logical_size(), 35);
        assert_eq!(m.version, Manifest::FORMAT_VERSION);
    }

    #[test]
    fn chunk_ref_from_bytes_hashes_and_sizes() {
        let bytes = b"hello chunk";
        let r = ChunkRef::from_bytes(bytes);
        assert_eq!(r.size, bytes.len() as u64);
        assert_eq!(r.digest.0, hex::encode(Sha256::digest(bytes)));
    }

    #[test]
    fn json_round_trips() {
        let original = Manifest::new(vec![
            ChunkRef::from_bytes(b"one"),
            ChunkRef::from_bytes(b"two"),
        ]);
        let bytes = original.to_bytes().expect("encode");
        let decoded = Manifest::from_bytes(&bytes).expect("decode");
        assert_eq!(decoded, original);
    }

    #[test]
    fn from_bytes_rejects_garbage() {
        let err = Manifest::from_bytes(b"not-json").expect_err("must fail");
        assert!(matches!(err, AppError::Other(_)));
    }

    #[test]
    fn map_range_full_object() {
        let m = two_chunks();
        let slices = map_range(&m, 0, 19).unwrap();
        assert_eq!(
            slices,
            vec![
                ChunkSlice {
                    digest: Digest("aa".into()),
                    offset: 0,
                    len: 10,
                },
                ChunkSlice {
                    digest: Digest("bb".into()),
                    offset: 0,
                    len: 10,
                },
            ]
        );
    }

    #[test]
    fn map_range_treats_end_as_inclusive() {
        let m = two_chunks();
        // Bytes [0, 9] → first chunk only, full 10 bytes.
        let slices = map_range(&m, 0, 9).unwrap();
        assert_eq!(
            slices,
            vec![ChunkSlice {
                digest: Digest("aa".into()),
                offset: 0,
                len: 10,
            }]
        );
        // Bytes [5, 14] → suffix of first + prefix of second.
        let slices = map_range(&m, 5, 14).unwrap();
        assert_eq!(
            slices,
            vec![
                ChunkSlice {
                    digest: Digest("aa".into()),
                    offset: 5,
                    len: 5,
                },
                ChunkSlice {
                    digest: Digest("bb".into()),
                    offset: 0,
                    len: 5,
                },
            ]
        );
    }

    #[test]
    fn map_range_single_byte_and_second_chunk_only() {
        let m = two_chunks();
        assert_eq!(
            map_range(&m, 3, 3).unwrap(),
            vec![ChunkSlice {
                digest: Digest("aa".into()),
                offset: 3,
                len: 1,
            }]
        );
        assert_eq!(
            map_range(&m, 10, 19).unwrap(),
            vec![ChunkSlice {
                digest: Digest("bb".into()),
                offset: 0,
                len: 10,
            }]
        );
    }

    #[test]
    fn map_range_past_end_yields_empty() {
        let m = two_chunks();
        assert!(map_range(&m, 20, 29).unwrap().is_empty());
        assert!(map_range(&Manifest::new(vec![]), 0, 0).unwrap().is_empty());
    }

    #[tokio::test]
    async fn commit_then_load_round_trips() {
        let root = TempDir::new().unwrap();
        let (_lifecycle, store) = setup(root.path());
        let manifest = Manifest::new(vec![
            ChunkRef::from_bytes(b"alpha"),
            ChunkRef::from_bytes(b"beta"),
        ]);

        let digest = commit_manifest(&store, &manifest).await.expect("commit");
        assert!(store.contains(&digest).await);

        let loaded = load_manifest(&store, &digest).await.expect("load");
        assert_eq!(loaded, manifest);
    }

    #[tokio::test]
    async fn identical_manifests_dedup_to_one_blob() {
        let root = TempDir::new().unwrap();
        let (_lifecycle, store) = setup(root.path());
        let manifest = Manifest::new(vec![ChunkRef::from_bytes(b"same")]);

        let a = commit_manifest(&store, &manifest).await.unwrap();
        let b = commit_manifest(&store, &manifest).await.unwrap();
        assert_eq!(a, b);

        // Only one CAS object under objects/ (fan-out dirs aside).
        let mut blob_files = 0usize;
        let mut stack = vec![store.objects_root().to_path_buf()];
        while let Some(dir) = stack.pop() {
            let mut rd = tokio::fs::read_dir(&dir).await.unwrap();
            while let Some(entry) = rd.next_entry().await.unwrap() {
                let ft = entry.file_type().await.unwrap();
                if ft.is_dir() {
                    stack.push(entry.path());
                } else if ft.is_file() {
                    blob_files += 1;
                }
            }
        }
        assert_eq!(blob_files, 1);
    }

    #[tokio::test]
    async fn chunk_digests_preserves_order() {
        let root = TempDir::new().unwrap();
        let (_lifecycle, store) = setup(root.path());
        let c1 = ChunkRef::from_bytes(b"first");
        let c2 = ChunkRef::from_bytes(b"second");
        let c3 = ChunkRef::from_bytes(b"third");
        let digest = commit_manifest(
            &store,
            &Manifest::new(vec![c1.clone(), c2.clone(), c3.clone()]),
        )
        .await
        .unwrap();

        let got = chunk_digests(&store, &digest).await.unwrap();
        assert_eq!(got, vec![c1.digest, c2.digest, c3.digest]);
    }

    #[tokio::test]
    async fn load_manifest_missing_is_no_such_key() {
        let root = TempDir::new().unwrap();
        let (_lifecycle, store) = setup(root.path());
        let missing = Digest(std::iter::repeat_n('0', 64).collect());
        let err = load_manifest(&store, &missing).await.expect_err("missing");
        assert!(matches!(err, AppError::NoSuchKey));
    }

    #[tokio::test]
    async fn open_range_reassembles_full_and_partial() {
        let root = TempDir::new().unwrap();
        let (lifecycle, store) = setup(root.path());

        let a = commit_chunk(&store, b"HELLO ").await; // 6
        let b = commit_chunk(&store, b"WORLD").await; // 5
        let logical = vec![a.clone(), b.clone()];
        let payload = b"HELLO WORLD";
        assert_eq!(a.size + b.size, payload.len() as u64);

        let manifest_digest = commit_manifest(&store, &Manifest::new(logical))
            .await
            .unwrap();

        let full = open_range(
            &store,
            &lifecycle,
            &manifest_digest,
            0,
            payload.len() as u64 - 1,
        )
        .await
        .unwrap();
        assert_eq!(read_all(full).await, payload);

        // "LO WOR" = inclusive bytes [3, 8] of "HELLO WORLD"
        let mid = open_range(&store, &lifecycle, &manifest_digest, 3, 8)
            .await
            .unwrap();
        assert_eq!(read_all(mid).await, b"LO WOR");
    }

    #[tokio::test]
    async fn open_range_works_for_cold_chunk() {
        let root = TempDir::new().unwrap();
        let (lifecycle, store) = setup(root.path());

        let chunk = b"cold-tier-chunk-bytes-for-cdc".repeat(4);
        let chunk_ref = commit_chunk(&store, &chunk).await;
        lifecycle
            .tier_blob(&chunk_ref.digest)
            .await
            .expect("tier chunk");

        let manifest_digest = commit_manifest(&store, &Manifest::new(vec![chunk_ref.clone()]))
            .await
            .unwrap();

        let reader = open_range(&store, &lifecycle, &manifest_digest, 0, chunk_ref.size - 1)
            .await
            .unwrap();
        assert_eq!(read_all(reader).await, chunk);
    }
}
