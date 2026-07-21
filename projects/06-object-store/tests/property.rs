//! Property-based tests for the whole object store.
//!
//! The per-module unit tests (`src/*.rs`) and `tests/http_api.rs` pin down
//! *specific* examples: this input → that output. Property tests attack the same
//! code from the other side — they assert an **invariant** ("for ALL inputs, X
//! holds") and let `proptest` generate hundreds of random inputs (and *shrink* a
//! failure to a minimal counterexample). They're the right tool exactly where a
//! vertical's correctness is a law over an infinite input space:
//!
//!   - **naming (path safety):** for *any* key, the encoding is a single, safe
//!     filename — the traversal defense can't be dodged by a clever key.
//!   - **store / streaming (content addressing):** the digest is `sha256(bytes)`
//!     and the ETag is `md5(bytes)` *no matter how the body is chunked*, and
//!     identical bytes always dedup to one blob.
//!   - **index (namespace, listing, GC):** put→get round-trips any key; paginated
//!     listing visits every matching key exactly once; GC reclaims exactly the
//!     unreferenced blobs.
//!   - **multipart (the S3 ETag):** parts assemble in part-number order and the
//!     `-N` ETag matches S3's formula, whatever order the parts arrived in.
//!
//! Async wrinkle: `proptest` test bodies are synchronous, so each case spins up a
//! throwaway current-thread runtime via [`block_on`] and drives the real async
//! store API inside it — the same code path the HTTP handlers use.

use bytes::Bytes;
use futures_util::stream;
use md5::Md5;
use object_store::error::AppError;
use object_store::index::{Index, NewVersion, Precondition};
use object_store::multipart::{Multipart, PartETag};
use object_store::naming::{encode_key, Bucket, Key};
use object_store::object::{Digest, ETag, ObjectRef};
use object_store::store::Store;
use object_store::streaming::{stream_to_store, Stored};
use proptest::prelude::*;
use proptest::test_runner::TestCaseError;
use sha2::{Digest as _, Sha256};
use std::sync::Arc;
use tempfile::TempDir;
use tokio::io::AsyncReadExt;

fn b(name: &str) -> Bucket {
    Bucket::from_trusted(name)
}
fn k(name: &str) -> Key {
    Key::from_trusted(name)
}

/// Run one async case to completion on a fresh current-thread runtime.
///
/// The store API is `async` (it does real filesystem I/O); `proptest` bodies are
/// sync. A current-thread runtime is cheap to build per case, and each case wants
/// its own isolated world anyway (its own `TempDir`), so there's nothing to share
/// across cases that would justify a global runtime.
fn block_on<F: std::future::Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build current-thread runtime")
        .block_on(fut)
}

/// Turn any `Debug` error (usually [`AppError`]) into a `proptest` failure, so an
/// unexpected `Err` on a should-succeed call reports as a shrunk counterexample
/// instead of a bare `unwrap` panic.
fn fail<E: std::fmt::Debug>(e: E) -> TestCaseError {
    TestCaseError::fail(format!("{e:?}"))
}

fn fresh_store() -> (TempDir, Arc<Store>) {
    let dir = TempDir::new().expect("temp root");
    let store = Store::open(dir.path()).expect("open store");
    (dir, store)
}

fn fresh_index() -> (TempDir, Arc<Index>) {
    let dir = TempDir::new().expect("temp root");
    let store = Store::open(dir.path()).expect("open store");
    let index = Index::open(dir.path(), store).expect("open index");
    (dir, index)
}

fn fresh_full() -> (TempDir, Arc<Store>, Arc<Index>) {
    let dir = TempDir::new().expect("temp root");
    let store = Store::open(dir.path()).expect("open store");
    let index = Index::open(dir.path(), store.clone()).expect("open index");
    (dir, store, index)
}

fn fresh_multipart() -> (TempDir, Arc<Store>, Arc<Index>, Arc<Multipart>) {
    let dir = TempDir::new().expect("temp root");
    let store = Store::open(dir.path()).expect("open store");
    let index = Index::open(dir.path(), store.clone()).expect("open index");
    let backend = Arc::new(object_store::index_backend::IndexBackend::local(
        index.clone(),
    ));
    let multipart = Multipart::open(dir.path(), store.clone(), backend).expect("open mp");
    (dir, store, index, multipart)
}

/// The content address: hex SHA-256 of the bytes (V1's blob name).
fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// The single-PUT S3 ETag: hex MD5 of the bytes (V2/V4 per-part).
fn md5_hex(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(bytes))
}

fn digest_of(bytes: &[u8]) -> Digest {
    Digest(sha256_hex(bytes))
}

/// The multipart ETag, defined from scratch: `hex(md5(concat(raw part md5s)))-N`,
/// with the part MD5s concatenated as RAW 16-byte digests in part-number order.
/// This is the exact value `aws s3` computes — if the impl matches this for
/// arbitrary parts, it's wire-compatible.
fn multipart_etag(parts_in_order: &[Vec<u8>]) -> String {
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

/// Reverse of [`encode_key`]'s percent-escaping, used to prove the encoding is
/// lossless (and therefore injective — distinct keys can never collide on disk).
fn percent_decode(s: &str) -> Vec<u8> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hi = (bytes[i + 1] as char).to_digit(16).expect("hi hex nibble");
            let lo = (bytes[i + 2] as char).to_digit(16).expect("lo hex nibble");
            out.push((hi * 16 + lo) as u8);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    out
}

/// A body stream shaped like the one axum hands a handler: a sequence of chunks.
fn body(
    chunks: Vec<Vec<u8>>,
) -> impl futures_util::Stream<Item = Result<Bytes, axum::Error>> + Unpin {
    stream::iter(
        chunks
            .into_iter()
            .map(|c| Ok(Bytes::from(c)))
            .collect::<Vec<_>>(),
    )
}

/// Slice `data` into fixed-size chunks (last one short) — a concrete "chunking".
fn rechunk(data: &[u8], chunk: usize) -> Vec<Vec<u8>> {
    data.chunks(chunk.max(1)).map(<[u8]>::to_vec).collect()
}

/// Stream `bytes` through V2 into the store as a single chunk; hand back what was
/// stored (digest / etag / size).
async fn store_bytes(store: &Store, bytes: &[u8]) -> Stored {
    stream_to_store(store, body(vec![bytes.to_vec()]), u64::MAX, None)
        .await
        .expect("storing bytes should succeed")
}

/// Read a committed blob back by digest.
async fn read_blob(store: &Store, digest: &Digest) -> Vec<u8> {
    let mut file = store.open_blob(digest).await.expect("open committed blob");
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).await.expect("read blob");
    bytes
}

/// Count committed blobs on disk (`objects/ab/cd/<64-hex>`).
fn blob_count(store: &Store) -> usize {
    fn walk(dir: &std::path::Path, n: &mut usize) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, n);
            } else if path.is_file() {
                *n += 1;
            }
        }
    }
    let mut n = 0;
    walk(store.objects_root(), &mut n);
    n
}

/// Number of files loitering in the store's `tmp/` — must be 0 after any commit
/// or any rejected/interrupted write.
fn temp_count(store: &Store) -> usize {
    std::fs::read_dir(store.tmp_dir())
        .map(std::iter::Iterator::count)
        .unwrap_or(0)
}

/// Put an index row pointing `(bucket, key)` at an already-stored blob.
async fn put_stored(
    index: &Index,
    bucket: &str,
    key: &str,
    stored: &Stored,
) -> Result<(), AppError> {
    index
        .put(
            &b(bucket),
            &k(key),
            NewVersion {
                digest: stored.digest.clone(),
                etag: stored.etag.clone(),
                size: stored.size,
                content_type: "application/octet-stream".into(),
                blob_kind: object_store::object::BlobKind::Whole,
            },
            Precondition::None,
        )
        .await?;
    Ok(())
}

/// Synthetic put — `put`/`get`/`list` only move the JSON pointer, so a made-up
/// digest is enough.
async fn put_sample(index: &Index, bucket: &str, key: &str, seed: usize) -> Result<(), AppError> {
    index
        .put(
            &b(bucket),
            &k(key),
            NewVersion {
                digest: Digest(format!("{seed:064x}")),
                etag: ETag(format!("etag-{seed}")),
                size: seed as u64,
                content_type: "application/octet-stream".into(),
                blob_kind: object_store::object::BlobKind::Whole,
            },
            Precondition::None,
        )
        .await?;
    Ok(())
}

/// Four distinct contents keyed by a small group id → at most 4 distinct blobs,
/// so duplicates genuinely share (exercising dedup + GC refcounting).
fn group_bytes(group: u8) -> Vec<u8> {
    format!("content-for-group-{group}").into_bytes()
}

// ── strategies ────────────────────────────────────────────────────────────────

/// Any Unicode key at all — control chars, slashes, emoji. This is the torture
/// input for the pure encoding rules (it never touches the filesystem).
fn any_key() -> impl Strategy<Value = String> {
    prop::collection::vec(any::<char>(), 0..40).prop_map(|v| v.into_iter().collect())
}

/// A filesystem-facing key: printable ASCII, bounded so the percent-encoded
/// filename (≤ 3× the byte length, plus `.json`) stays well under the 255-byte
/// name limit. Still exercises `/`, `.`, `%`, spaces — the interesting cases.
fn fs_key() -> impl Strategy<Value = String> {
    "[a-zA-Z0-9/._~ %+=-]{1,30}"
}

/// A valid S3 bucket name by construction: 3–63 chars, `[a-z0-9-]`, and neither
/// end a hyphen.
fn valid_bucket() -> impl Strategy<Value = String> {
    "[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
}

/// 0–12 chunks of 0–64 random bytes — an arbitrary streamed body, split
/// arbitrarily. The `.concat()` of these is the logical object.
fn byte_chunks() -> impl Strategy<Value = Vec<Vec<u8>>> {
    prop::collection::vec(prop::collection::vec(any::<u8>(), 0..64), 0..12)
}

/// A multipart upload: 1–6 parts of 0–40 bytes, plus a shuffled arrival order.
/// Uploading in `order` (not part-number order) is the whole point — assembly
/// and the ETag must depend on part *number*, never arrival.
fn multipart_case() -> impl Strategy<Value = (Vec<Vec<u8>>, Vec<usize>)> {
    prop::collection::vec(prop::collection::vec(any::<u8>(), 0..40), 1..7).prop_flat_map(|parts| {
        let n = parts.len();
        let order = Just((0..n).collect::<Vec<usize>>()).prop_shuffle();
        (Just(parts), order)
    })
}

// ══ V-naming: path safety, from any key ═══════════════════════════════════════

proptest! {
    /// The single most important security invariant: `encode_key` flattens the
    /// keyspace so a key can NEVER name more than one directory segment. For any
    /// key whatsoever, the encoding contains no `/` — no `../../etc/passwd` can
    /// climb out of the bucket dir.
    #[test]
    fn encode_key_never_emits_a_slash(key in any_key()) {
        let encoded = encode_key(&key);
        prop_assert!(
            !encoded.contains('/'),
            "encoded key {encoded:?} must never contain '/'"
        );
    }

    /// The encoding is a valid single filename: every byte is an unreserved char
    /// or part of a `%XX` escape. Nothing that could be a path separator or a
    /// shell/glob surprise survives.
    #[test]
    fn encode_key_output_is_a_safe_filename(key in any_key()) {
        let encoded = encode_key(&key);
        prop_assert!(
            encoded
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~' | b'%')),
            "encoded key {encoded:?} contains a byte outside the safe set"
        );
    }

    /// The encoding is lossless — percent-decoding recovers the exact key bytes.
    /// Losslessness ⇒ injective: two distinct keys can't collapse to the same
    /// on-disk filename, so no object ever silently clobbers another.
    #[test]
    fn encode_key_is_reversible(key in any_key()) {
        prop_assert_eq!(
            percent_decode(&encode_key(&key)),
            key.as_bytes(),
            "percent-decoding the encoding must recover the original key"
        );
    }

    /// Unreserved characters pass through untouched — a plain key like
    /// `photos/2024.jpg`'s unreserved parts aren't needlessly mangled.
    #[test]
    fn encode_key_is_identity_on_unreserved(s in "[A-Za-z0-9._~-]{0,40}") {
        prop_assert_eq!(encode_key(&s), s, "unreserved chars must pass through unchanged");
    }

    /// Every constructively-valid S3 name is accepted.
    #[test]
    fn validate_accepts_all_well_formed_bucket_names(name in valid_bucket()) {
        prop_assert!(
            Bucket::new(&name).is_ok(),
            "{name:?} is a well-formed S3 bucket name and must be accepted"
        );
    }

    /// Injecting a single illegal character into an otherwise-fine name is always
    /// rejected — the charset whitelist is the traversal defense, so it must have
    /// no gaps.
    #[test]
    fn validate_rejects_any_illegal_character(
        base in "[a-z0-9]{2,40}",
        bad in prop::sample::select(vec!['/', '.', '_', ' ', 'A', 'Z', '~', '*', '\\']),
        pos in any::<prop::sample::Index>(),
    ) {
        let mut chars: Vec<char> = base.chars().collect();
        let at = pos.index(chars.len() + 1);
        chars.insert(at, bad);
        let name: String = chars.into_iter().collect();
        // base is ≥2 chars, so after insertion length is ≥3 — the rejection is
        // due to the illegal char, not the length rule.
        prop_assert!(
            matches!(Bucket::new(&name), Err(AppError::InvalidRequest(_))),
            "{name:?} contains an illegal char and must be rejected"
        );
    }
}

// ══ V1: content addressing — blob_path ⇄ digest_from_path ══════════════════════

proptest! {
    /// The content address round-trips: `blob_path` fans a digest out to
    /// `objects/ab/cd/<64-hex>`, and `digest_from_path` recovers exactly that
    /// digest. The filename IS the digest, and the mapping is deterministic.
    #[test]
    fn blob_path_round_trips_any_digest(hexd in "[0-9a-f]{64}") {
        let (_dir, store) = fresh_store();
        let path = store.blob_path(&Digest(hexd.clone()));

        prop_assert_eq!(
            path.file_name().and_then(|n| n.to_str()),
            Some(hexd.as_str()),
            "the blob filename must be the digest itself"
        );
        prop_assert_eq!(
            store.digest_from_path(&path),
            Some(Digest(hexd.clone())),
            "digest_from_path must invert blob_path"
        );
        prop_assert_eq!(
            store.blob_path(&Digest(hexd.clone())),
            path,
            "blob_path must be deterministic for a given digest"
        );
    }
}

// ══ V2: streaming — chunk-boundary independence, size cap, dedup ═══════════════

proptest! {
    #![proptest_config(ProptestConfig::with_cases(48))]

    /// The heart of V2: whatever chunk boundaries the body arrives on, the
    /// committed blob has digest = `sha256(bytes)`, etag = `md5(bytes)`, and
    /// size = the true byte count — and reads back byte-for-byte. If any of these
    /// depended on chunking, the streaming loop would be buggy.
    #[test]
    fn stream_commits_content_address_regardless_of_chunking(chunks in byte_chunks()) {
        block_on(async {
            let (_dir, store) = fresh_store();
            let data = chunks.concat();
            let stored = stream_to_store(&store, body(chunks), u64::MAX, None)
                .await
                .map_err(fail)?;

            let (expected_digest, expected_etag) = (sha256_hex(&data), md5_hex(&data));
            prop_assert_eq!(stored.digest.as_str(), expected_digest.as_str(), "digest = sha256(bytes)");
            prop_assert_eq!(stored.etag.as_str(), expected_etag.as_str(), "etag = md5(bytes)");
            prop_assert_eq!(stored.size, data.len() as u64, "size = true byte count");
            prop_assert!(store.contains(&stored.digest).await, "blob committed under its digest");
            prop_assert_eq!(read_blob(&store, &stored.digest).await, data, "blob reads back byte-for-byte");
            prop_assert_eq!(temp_count(&store), 0, "commit consumes the temp file");
            Ok::<(), TestCaseError>(())
        })?;
    }

    /// The size cap is exact and inclusive: a body at exactly `max_size` is
    /// accepted; one byte over is `EntityTooLarge` and leaks no temp file. The
    /// cap is on the running total, so this holds however the bytes are chunked.
    #[test]
    fn stream_size_cap_is_exact_and_leak_free(chunks in byte_chunks()) {
        block_on(async {
            let data = chunks.concat();
            let total = data.len() as u64;

            let (_dir, store) = fresh_store();
            prop_assert!(
                stream_to_store(&store, body(chunks.clone()), total, None).await.is_ok(),
                "a body exactly at the cap must be accepted"
            );

            if total > 0 {
                let (_dir2, store2) = fresh_store();
                let outcome = stream_to_store(&store2, body(chunks), total - 1, None).await;
                prop_assert!(
                    matches!(outcome, Err(AppError::EntityTooLarge)),
                    "a body one byte over the cap must be EntityTooLarge"
                );
                prop_assert_eq!(temp_count(&store2), 0, "a rejected upload must leak no temp file");
                prop_assert_eq!(blob_count(&store2), 0, "a rejected upload must commit no blob");
            }
            Ok::<(), TestCaseError>(())
        })?;
    }

    /// Dedup is real for arbitrary bytes: streaming the same content twice — even
    /// split into completely different chunks — yields the same digest and leaves
    /// exactly ONE blob on disk.
    #[test]
    fn identical_bytes_dedup_to_one_blob(
        data in prop::collection::vec(any::<u8>(), 0..160),
        chunk_a in 1usize..9,
        chunk_b in 1usize..9,
    ) {
        block_on(async {
            let (_dir, store) = fresh_store();
            let a = stream_to_store(&store, body(rechunk(&data, chunk_a)), u64::MAX, None).await.map_err(fail)?;
            let b = stream_to_store(&store, body(rechunk(&data, chunk_b)), u64::MAX, None).await.map_err(fail)?;

            prop_assert_eq!(a.digest.as_str(), b.digest.as_str(), "same bytes → same digest, any chunking");
            prop_assert_eq!(blob_count(&store), 1, "identical content must dedup to a single blob");
            Ok::<(), TestCaseError>(())
        })?;
    }
}

// ══ V3: index — put/get round-trip, listing completeness, GC refcounting ══════

proptest! {
    #![proptest_config(ProptestConfig::with_cases(40))]

    /// put → get round-trips the full metadata for ANY key — including keys full
    /// of `/`, `.`, and `%`, which must be stored flat (percent-encoded to one
    /// filename), never as a nested directory tree.
    #[test]
    fn put_then_get_round_trips_any_key(key in fs_key(), seed in 0usize..1_000_000) {
        block_on(async {
            let (_dir, index) = fresh_index();
            index.create_bucket(&b("photos")).await.map_err(fail)?;

            put_sample(&index, "photos", &key, seed).await.map_err(fail)?;

            let got = index
                .resolve(&b("photos"), &k(&key), ObjectRef::Latest)
                .await
                .map_err(fail)?;

            prop_assert_eq!(got.key.as_str(), key.as_str(), "key round-trips");
            prop_assert_eq!(got.digest, Digest(format!("{seed:064x}")), "digest round-trips");
            prop_assert_eq!(got.size, seed as u64, "size round-trips");
            prop_assert_eq!(got.content_type.as_str(), "application/octet-stream", "content type round-trips");
            Ok::<(), TestCaseError>(())
        })?;
    }

    /// Paginated listing (no delimiter) is a pure, complete walk: for any set of
    /// keys, any prefix, and any `max-keys`, following the continuation token to
    /// the end visits EXACTLY the prefix-matching keys, each once, in sorted
    /// order — and no page ever exceeds `max-keys`.
    #[test]
    fn pagination_visits_every_matching_key_exactly_once(
        keys in prop::collection::hash_set(fs_key(), 0..16),
        max_keys in 1usize..6,
        prefix in "[a-zA-Z0-9/]{0,3}",
    ) {
        block_on(async {
            let (_dir, index) = fresh_index();
            index.create_bucket(&b("photos")).await.map_err(fail)?;
            for (i, key) in keys.iter().enumerate() {
                put_sample(&index, "photos", key, i).await.map_err(fail)?;
            }

            let mut seen: Vec<String> = Vec::new();
            let mut token: Option<String> = None;
            let mut pages = 0usize;
            loop {
                let page = index
                    .list(&b("photos"), &prefix, None, token.as_deref(), max_keys)
                    .await
                    .map_err(fail)?;
                prop_assert!(page.objects.len() <= max_keys, "no page may exceed max_keys");
                prop_assert!(page.common_prefixes.is_empty(), "no delimiter → no common prefixes");
                seen.extend(page.objects.iter().map(|m| m.key.to_string()));

                pages += 1;
                prop_assert!(pages <= keys.len() + 2, "pagination must terminate");

                match page.next_continuation_token {
                    Some(t) => token = Some(t),
                    None => break,
                }
            }

            let mut expected: Vec<String> =
                keys.iter().filter(|k| k.starts_with(&prefix)).cloned().collect();
            expected.sort();
            prop_assert_eq!(seen, expected, "pagination must visit each matching key once, sorted");
            Ok::<(), TestCaseError>(())
        })?;
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(24))]

    /// GC is never destructive to live data: after deleting an arbitrary subset of
    /// keys, EVERY key that still exists must still resolve to a present blob — GC
    /// must never reap bytes a live key points at (that's silent data loss). When
    /// two keys share a blob, deleting one must leave the other's bytes intact.
    ///
    /// This is the *safety* half of the mark-sweep law, and it holds regardless of
    /// the GC grace window. The *reclamation* half — an orphaned blob is eventually
    /// removed — needs the zero grace window that only exists under `cfg!(test)`,
    /// which is active for the library's own unit tests but NOT for this external
    /// integration crate (here the real 60s grace applies, so a just-orphaned blob
    /// is intentionally still present). That direction is property-tested in-crate
    /// in `src/index.rs::tests::prop_gc_reclaims_exactly_unreferenced_blobs`.
    #[test]
    fn gc_never_reaps_a_referenced_blob(
        entries in prop::collection::hash_map(fs_key(), (0u8..4, any::<bool>()), 0..12),
    ) {
        block_on(async {
            let (_dir, store, index) = fresh_full();
            index.create_bucket(&b("photos")).await.map_err(fail)?;

            for (key, (group, _)) in &entries {
                let stored = store_bytes(&store, &group_bytes(*group)).await;
                put_stored(&index, "photos", key, &stored).await.map_err(fail)?;
            }
            for (key, (_, delete)) in &entries {
                if *delete {
                    index
                        .delete(&b("photos"), &k(key), ObjectRef::Latest)
                        .await
                        .map_err(fail)?;
                }
            }

            index.gc().await.map_err(fail)?;

            // Every surviving key must still resolve to bytes that are on disk.
            for (key, (group, delete)) in &entries {
                if *delete {
                    continue;
                }
                let got = index
                    .resolve(&b("photos"), &k(key), ObjectRef::Latest)
                    .await
                    .map_err(fail)?;
                let present = store.contains(&got.digest).await;
                prop_assert!(present, "a live key's blob must survive GC");
                prop_assert_eq!(got.digest, digest_of(&group_bytes(*group)), "digest matches its group");
            }
            Ok::<(), TestCaseError>(())
        })?;
    }
}

// ══ V4: multipart — order-independent assembly + the S3 `-N` ETag ═════════════

proptest! {
    #![proptest_config(ProptestConfig::with_cases(24))]

    /// The multipart law: whatever order parts arrive in, `complete` assembles
    /// them in part-number order, and the object's ETag is S3's
    /// `hex(md5(concat(raw part md5s)))-N`. Each part's own ETag is the MD5 of its
    /// bytes. This is the concrete, testable definition of "S3-compatible".
    #[test]
    fn multipart_assembles_in_order_with_the_s3_etag((parts, order) in multipart_case()) {
        block_on(async {
            let (_dir, store, index, mp) = fresh_multipart();
            index.create_bucket(&b("photos")).await.map_err(fail)?;
            let upload_id = mp
                .initiate(&b("photos"), &k("big.bin"), "application/octet-stream".into())
                .await
                .map_err(fail)?;

            // Stream parts in the shuffled arrival order; part N gets number N+1.
            let mut part_etags: Vec<PartETag> = Vec::new();
            for &i in &order {
                let pe = mp
                    .upload_part(&upload_id, (i as u32) + 1, body(vec![parts[i].clone()]), u64::MAX)
                    .await
                    .map_err(fail)?;
                let part_md5 = md5_hex(&parts[i]);
                prop_assert_eq!(
                    pe.etag.as_str(),
                    part_md5.as_str(),
                    "a part's ETag must be the MD5 of its bytes"
                );
                part_etags.push(pe);
            }

            let meta = mp.complete(&upload_id, part_etags).await.map_err(fail)?;
            let live = meta
                .latest_live()
                .ok_or_else(|| TestCaseError::fail("completed object must be live"))?;

            let whole = parts.concat();
            prop_assert_eq!(
                read_blob(&store, &live.digest).await,
                whole.clone(),
                "parts must concatenate in part-number order, not arrival order"
            );
            prop_assert_eq!(live.size, whole.len() as u64, "size is the assembled byte count");
            let expected_etag = multipart_etag(&parts);
            prop_assert_eq!(
                live.etag.as_str(),
                expected_etag.as_str(),
                "the multipart ETag must match S3's -N formula"
            );
            prop_assert!(
                live.etag.as_str().ends_with(&format!("-{}", parts.len())),
                "the -N suffix must carry the part count"
            );
            Ok::<(), TestCaseError>(())
        })?;
    }
}
