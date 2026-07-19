//! Reference-model checking (the ShardStore method).
//!
//! A tiny in-memory oracle and the real [`Index`] + [`Store`] are driven by the
//! same random op sequence. After every step their **observable** results must
//! agree — success payloads and error *kinds*, not internal layout.
//!
//! This is differential / oracle testing, not a named invariant: any history
//! proptest can invent must keep the real store behaviourally identical to the
//! model. See SPEC "Durability & correctness practice" and RESEARCH.md §Part 2 & 8.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use bytes::Bytes;
use futures_util::stream;
use md5::Md5;
use object_store::error::AppError;
use object_store::index::{Index, Listing, NewVersion, Precondition};
use object_store::naming::validate_bucket_name;
use object_store::object::{Digest, ETag, ObjectRef};
use object_store::store::Store;
use object_store::streaming::{stream_to_store, Stored};
use proptest::prelude::*;
use proptest::test_runner::TestCaseError;
use sha2::{Digest as _, Sha256};
use tempfile::TempDir;
use tokio::io::AsyncReadExt;

// ── harness plumbing ──────────────────────────────────────────────────────────

fn block_on<F: std::future::Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build current-thread runtime")
        .block_on(fut)
}

fn fail(msg: impl Into<String>) -> TestCaseError {
    TestCaseError::fail(msg.into())
}

fn fresh_full() -> (TempDir, Arc<Store>, Arc<Index>) {
    let dir = TempDir::new().expect("temp root");
    let store = Store::open(dir.path()).expect("open store");
    let index = Index::open(dir.path(), store.clone()).expect("open index");
    (dir, store, index)
}

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

async fn store_bytes(store: &Store, bytes: &[u8]) -> Result<Stored, AppError> {
    stream_to_store(store, body(vec![bytes.to_vec()]), u64::MAX, None).await
}

async fn read_blob(store: &Store, digest: &Digest) -> Result<Vec<u8>, AppError> {
    let mut file = store.open_blob(digest).await?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).await?;
    Ok(bytes)
}

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn md5_hex(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(bytes))
}

fn valid_bucket() -> impl Strategy<Value = String> {
    "[a-z0-9][a-z0-9-]{1,21}[a-z0-9]"
}

fn fs_key() -> impl Strategy<Value = String> {
    "[a-zA-Z0-9/._~ %+=-]{1,24}"
}

// ── observables ───────────────────────────────────────────────────────────────

/// Error kinds the model and real store must agree on (not Display strings).
#[derive(Debug, Clone, PartialEq, Eq)]
enum ErrKind {
    InvalidRequest,
    BucketAlreadyExists,
    PreconditionFailed,
    NoSuchKey,
    NoSuchBucket,
}

fn err_kind(e: &AppError) -> Result<ErrKind, TestCaseError> {
    match e {
        AppError::InvalidRequest(_) => Ok(ErrKind::InvalidRequest),
        AppError::BucketAlreadyExists => Ok(ErrKind::BucketAlreadyExists),
        AppError::PreconditionFailed => Ok(ErrKind::PreconditionFailed),
        AppError::NoSuchKey => Ok(ErrKind::NoSuchKey),
        AppError::NoSuchBucket => Ok(ErrKind::NoSuchBucket),
        other => Err(fail(format!("unexpected real-store error: {other:?}"))),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ListPageObs {
    /// `(key, etag, size)` for leaf objects on this page, in list order.
    objects: Vec<(String, String, u64)>,
    common_prefixes: Vec<String>,
    /// Continuation token for the next page (`None` when this page ends the list).
    next_token: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Obs {
    Ok,
    Put {
        etag: String,
        size: u64,
        content_type: String,
    },
    Get {
        etag: String,
        digest: String,
        size: u64,
        content_type: String,
        body: Vec<u8>,
    },
    /// `resolve(Latest)` → [`AppError::NoSuchKey`].
    GetMissing,
    List {
        pages: Vec<ListPageObs>,
    },
    Err(ErrKind),
}

// ── in-memory model ───────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
struct LiveObject {
    body: Vec<u8>,
    digest: String,
    etag: String,
    size: u64,
    content_type: String,
}

#[derive(Debug, Default)]
struct Model {
    buckets: HashSet<String>,
    /// Live latest only — matches what Get/List expose for this cut.
    objects: HashMap<(String, String), LiveObject>,
}

impl Model {
    fn live(&self, bucket: &str, key: &str) -> Option<&LiveObject> {
        self.objects.get(&(bucket.to_string(), key.to_string()))
    }

    fn create_bucket(&mut self, bucket: &str) -> Obs {
        if validate_bucket_name(bucket).is_err() {
            return Obs::Err(ErrKind::InvalidRequest);
        }
        if !self.buckets.insert(bucket.to_string()) {
            return Obs::Err(ErrKind::BucketAlreadyExists);
        }
        Obs::Ok
    }

    fn put(
        &mut self,
        bucket: &str,
        key: &str,
        body: &[u8],
        content_type: &str,
        pre: Precondition,
    ) -> Obs {
        if validate_bucket_name(bucket).is_err() || key.is_empty() {
            return Obs::Err(ErrKind::InvalidRequest);
        }

        let live = self.live(bucket, key);
        match &pre {
            Precondition::None => {}
            Precondition::IfMatch(expected) => match live {
                Some(obj) if obj.etag == expected.as_str() => {}
                _ => return Obs::Err(ErrKind::PreconditionFailed),
            },
            Precondition::IfNoneMatchStar => {
                if live.is_some() {
                    return Obs::Err(ErrKind::PreconditionFailed);
                }
            }
        }

        // Index::put creates the bucket directory as a side effect of the
        // durable publish dance — the model mirrors that by recording the name.
        self.buckets.insert(bucket.to_string());

        let digest = sha256_hex(body);
        let etag = md5_hex(body);
        let size = body.len() as u64;
        self.objects.insert(
            (bucket.to_string(), key.to_string()),
            LiveObject {
                body: body.to_vec(),
                digest: digest.clone(),
                etag: etag.clone(),
                size,
                content_type: content_type.to_string(),
            },
        );
        Obs::Put {
            etag,
            size,
            content_type: content_type.to_string(),
        }
    }

    fn get(&self, bucket: &str, key: &str) -> Obs {
        if validate_bucket_name(bucket).is_err() || key.is_empty() {
            return Obs::Err(ErrKind::InvalidRequest);
        }
        match self.live(bucket, key) {
            Some(obj) => Obs::Get {
                etag: obj.etag.clone(),
                digest: obj.digest.clone(),
                size: obj.size,
                content_type: obj.content_type.clone(),
                body: obj.body.clone(),
            },
            None => Obs::GetMissing,
        }
    }

    fn delete(&mut self, bucket: &str, key: &str) -> Obs {
        if validate_bucket_name(bucket).is_err() || key.is_empty() {
            return Obs::Err(ErrKind::InvalidRequest);
        }
        self.objects.remove(&(bucket.to_string(), key.to_string()));
        Obs::Ok
    }

    /// One ListObjectsV2-style page — algorithm mirrors [`Index::list`].
    fn list_page(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        continuation: Option<&str>,
        max_keys: usize,
    ) -> Result<ListPageObs, ErrKind> {
        if validate_bucket_name(bucket).is_err() {
            return Err(ErrKind::InvalidRequest);
        }

        let mut pairs: Vec<(&str, &LiveObject)> = self
            .objects
            .iter()
            .filter(|((b, k), _)| b.as_str() == bucket && k.starts_with(prefix))
            .map(|((_, k), obj)| (k.as_str(), obj))
            .collect();
        pairs.sort_by(|a, b| a.0.cmp(b.0));

        let mut leaf_keys: Vec<(&str, &LiveObject)> = Vec::new();
        let mut common_prefixes: Vec<String> = Vec::new();
        if let Some(delim) = delimiter {
            let mut rolled = HashSet::new();
            for (key, obj) in pairs {
                let remainder = key.strip_prefix(prefix).unwrap_or(key);
                if let Some(idx) = remainder.find(delim) {
                    let end = idx + delim.len();
                    rolled.insert(format!("{}{}", prefix, &remainder[..end]));
                } else {
                    leaf_keys.push((key, obj));
                }
            }
            common_prefixes = rolled.into_iter().collect();
            common_prefixes.sort();
        } else {
            leaf_keys = pairs;
        }

        #[derive(Clone)]
        enum Item<'a> {
            Object(&'a str, &'a LiveObject),
            Prefix(String),
        }
        impl Item<'_> {
            fn sort_key(&self) -> &str {
                match self {
                    Item::Object(k, _) => k,
                    Item::Prefix(p) => p.as_str(),
                }
            }
        }

        let mut items: Vec<Item<'_>> = leaf_keys
            .into_iter()
            .map(|(k, o)| Item::Object(k, o))
            .chain(common_prefixes.into_iter().map(Item::Prefix))
            .collect();
        items.sort_by(|a, b| a.sort_key().cmp(b.sort_key()));

        if let Some(token) = continuation {
            items.retain(|item| item.sort_key() > token);
        }

        let next_token = if items.len() > max_keys {
            Some(items[max_keys - 1].sort_key().to_string())
        } else {
            None
        };
        items.truncate(max_keys);

        let mut page_objects = Vec::new();
        let mut page_prefixes = Vec::new();
        for item in items {
            match item {
                Item::Object(k, o) => {
                    page_objects.push((k.to_string(), o.etag.clone(), o.size));
                }
                Item::Prefix(p) => page_prefixes.push(p),
            }
        }

        Ok(ListPageObs {
            objects: page_objects,
            common_prefixes: page_prefixes,
            next_token,
        })
    }

    fn list_all(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        max_keys: usize,
    ) -> Obs {
        let mut pages = Vec::new();
        let mut continuation: Option<String> = None;
        loop {
            let page = match self.list_page(
                bucket,
                prefix,
                delimiter,
                continuation.as_deref(),
                max_keys,
            ) {
                Ok(p) => p,
                Err(e) => return Obs::Err(e),
            };
            let next = page.next_token.clone();
            pages.push(page);
            match next {
                Some(token) => continuation = Some(token),
                None => break,
            }
        }
        Obs::List { pages }
    }
}

// ── ops ───────────────────────────────────────────────────────────────────────

/// How to build a [`Precondition`] at apply time (so both sides share one value).
#[derive(Debug, Clone)]
enum PreconSpec {
    None,
    IfNoneMatchStar,
    /// Use the model's current live etag, or a bogus one if the key is absent.
    IfMatchCurrent,
    IfMatchBogus,
}

#[derive(Debug, Clone)]
enum Op {
    CreateBucket {
        bucket_idx: usize,
    },
    Put {
        bucket_idx: usize,
        key_idx: usize,
        body: Vec<u8>,
        precon: PreconSpec,
    },
    Get {
        bucket_idx: usize,
        key_idx: usize,
    },
    Delete {
        bucket_idx: usize,
        key_idx: usize,
    },
    List {
        bucket_idx: usize,
        /// Prefix drawn from a key in the pool (or empty).
        prefix_from_key: Option<usize>,
        /// If true, use `"/"` as delimiter.
        use_delimiter: bool,
        max_keys: usize,
    },
}

fn resolve_precon(model: &Model, bucket: &str, key: &str, spec: &PreconSpec) -> Precondition {
    match spec {
        PreconSpec::None => Precondition::None,
        PreconSpec::IfNoneMatchStar => Precondition::IfNoneMatchStar,
        PreconSpec::IfMatchCurrent => {
            let etag = model
                .live(bucket, key)
                .map(|o| o.etag.clone())
                .unwrap_or_else(|| "no-such-etag".into());
            Precondition::IfMatch(ETag(etag))
        }
        PreconSpec::IfMatchBogus => Precondition::IfMatch(ETag("deadbeef-not-a-real-etag".into())),
    }
}

fn listing_to_page(listing: Listing) -> ListPageObs {
    ListPageObs {
        objects: listing
            .objects
            .into_iter()
            .filter_map(|meta| {
                let live = meta.latest_live()?;
                Some((meta.key, live.etag.0, live.size))
            })
            .collect(),
        common_prefixes: listing.common_prefixes,
        next_token: listing.next_continuation_token,
    }
}

// ── apply ─────────────────────────────────────────────────────────────────────

const CONTENT_TYPE: &str = "application/octet-stream";

fn apply_model(model: &mut Model, buckets: &[String], keys: &[String], op: &Op) -> Obs {
    match op {
        Op::CreateBucket { bucket_idx } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            model.create_bucket(bucket)
        }
        Op::Put {
            bucket_idx,
            key_idx,
            body,
            precon,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            let pre = resolve_precon(model, bucket, key, precon);
            model.put(bucket, key, body, CONTENT_TYPE, pre)
        }
        Op::Get {
            bucket_idx,
            key_idx,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            model.get(bucket, key)
        }
        Op::Delete {
            bucket_idx,
            key_idx,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            model.delete(bucket, key)
        }
        Op::List {
            bucket_idx,
            prefix_from_key,
            use_delimiter,
            max_keys,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let prefix = prefix_from_key
                .map(|i| keys[i % keys.len()].as_str())
                .unwrap_or("");
            let delimiter = use_delimiter.then_some("/");
            model.list_all(bucket, prefix, delimiter, *max_keys)
        }
    }
}

async fn apply_real(
    store: &Store,
    index: &Index,
    model: &Model,
    buckets: &[String],
    keys: &[String],
    op: &Op,
) -> Result<Obs, TestCaseError> {
    match op {
        Op::CreateBucket { bucket_idx } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            match index.create_bucket(bucket).await {
                Ok(()) => Ok(Obs::Ok),
                Err(e) => Ok(Obs::Err(err_kind(&e)?)),
            }
        }
        Op::Put {
            bucket_idx,
            key_idx,
            body,
            precon,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            // Resolved against the pre-op model so both sides share one value.
            let pre = resolve_precon(model, bucket, key, precon);
            let stored = match store_bytes(store, body).await {
                Ok(s) => s,
                Err(e) => return Ok(Obs::Err(err_kind(&e)?)),
            };
            match index
                .put(
                    bucket,
                    key,
                    NewVersion {
                        digest: stored.digest,
                        etag: stored.etag.clone(),
                        size: stored.size,
                        content_type: CONTENT_TYPE.into(),
                    },
                    pre,
                )
                .await
            {
                Ok(meta) => {
                    let live = meta.latest_live().expect("put always leaves a live latest");
                    Ok(Obs::Put {
                        etag: live.etag.0,
                        size: live.size,
                        content_type: live.content_type,
                    })
                }
                Err(e) => Ok(Obs::Err(err_kind(&e)?)),
            }
        }
        Op::Get {
            bucket_idx,
            key_idx,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            match index.resolve(bucket, key, ObjectRef::Latest).await {
                Ok(resolved) => {
                    let body = read_blob(store, &resolved.digest)
                        .await
                        .map_err(|e| fail(format!("read blob after resolve: {e:?}")))?;
                    Ok(Obs::Get {
                        etag: resolved.etag.0,
                        digest: resolved.digest.0,
                        size: resolved.size,
                        content_type: resolved.content_type,
                        body,
                    })
                }
                Err(AppError::NoSuchKey) => Ok(Obs::GetMissing),
                Err(e) => Ok(Obs::Err(err_kind(&e)?)),
            }
        }
        Op::Delete {
            bucket_idx,
            key_idx,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let key = &keys[*key_idx % keys.len()];
            match index.delete(bucket, key, ObjectRef::Latest).await {
                Ok(()) => Ok(Obs::Ok),
                Err(e) => Ok(Obs::Err(err_kind(&e)?)),
            }
        }
        Op::List {
            bucket_idx,
            prefix_from_key,
            use_delimiter,
            max_keys,
        } => {
            let bucket = &buckets[*bucket_idx % buckets.len()];
            let prefix = prefix_from_key
                .map(|i| keys[i % keys.len()].as_str())
                .unwrap_or("");
            let delimiter = use_delimiter.then_some("/");
            let mut pages = Vec::new();
            let mut continuation: Option<String> = None;
            loop {
                let listing = match index
                    .list(bucket, prefix, delimiter, continuation.as_deref(), *max_keys)
                    .await
                {
                    Ok(l) => l,
                    Err(e) => return Ok(Obs::Err(err_kind(&e)?)),
                };
                let page = listing_to_page(listing);
                let next = page.next_token.clone();
                pages.push(page);
                match next {
                    Some(token) => continuation = Some(token),
                    None => break,
                }
            }
            Ok(Obs::List { pages })
        }
    }
}

fn assert_final_state(
    model: &Model,
    store: &Store,
    index: &Index,
) -> Result<(), TestCaseError> {
    let expected: BTreeMap<(String, String), &LiveObject> = model
        .objects
        .iter()
        .map(|((b, k), obj)| ((b.clone(), k.clone()), obj))
        .collect();

    for ((bucket, key), obj) in expected {
        let obs = block_on(async {
            match index.resolve(&bucket, &key, ObjectRef::Latest).await {
                Ok(resolved) => {
                    let body = read_blob(store, &resolved.digest)
                        .await
                        .map_err(|e| fail(format!("final read {bucket}/{key}: {e:?}")))?;
                    Ok::<_, TestCaseError>(Obs::Get {
                        etag: resolved.etag.0,
                        digest: resolved.digest.0,
                        size: resolved.size,
                        content_type: resolved.content_type,
                        body,
                    })
                }
                Err(AppError::NoSuchKey) => Ok(Obs::GetMissing),
                Err(e) => Ok(Obs::Err(err_kind(&e)?)),
            }
        })?;
        let want = Obs::Get {
            etag: obj.etag.clone(),
            digest: obj.digest.clone(),
            size: obj.size,
            content_type: obj.content_type.clone(),
            body: obj.body.clone(),
        };
        prop_assert_eq!(&obs, &want, "final state diverged for {}/{}", bucket, key);
    }
    Ok(())
}

// ── strategies ────────────────────────────────────────────────────────────────

fn precon_strategy() -> impl Strategy<Value = PreconSpec> {
    prop_oneof![
        3 => Just(PreconSpec::None),
        1 => Just(PreconSpec::IfNoneMatchStar),
        1 => Just(PreconSpec::IfMatchCurrent),
        1 => Just(PreconSpec::IfMatchBogus),
    ]
}

fn op_strategy(n_buckets: usize, n_keys: usize) -> impl Strategy<Value = Op> {
    let bucket = 0..n_buckets;
    let key = 0..n_keys;
    prop_oneof![
        bucket.clone().prop_map(|bucket_idx| Op::CreateBucket { bucket_idx }),
        (
            bucket.clone(),
            key.clone(),
            prop::collection::vec(any::<u8>(), 0..48),
            precon_strategy()
        )
            .prop_map(|(bucket_idx, key_idx, body, precon)| Op::Put {
                bucket_idx,
                key_idx,
                body,
                precon,
            }),
        (bucket.clone(), key.clone()).prop_map(|(bucket_idx, key_idx)| Op::Get {
            bucket_idx,
            key_idx,
        }),
        (bucket.clone(), key.clone()).prop_map(|(bucket_idx, key_idx)| Op::Delete {
            bucket_idx,
            key_idx,
        }),
        (
            bucket,
            prop::option::of(0..n_keys),
            any::<bool>(),
            1usize..6
        )
            .prop_map(
                |(bucket_idx, prefix_from_key, use_delimiter, max_keys)| Op::List {
                    bucket_idx,
                    prefix_from_key,
                    use_delimiter,
                    max_keys,
                }
            ),
    ]
}

fn scenario_strategy() -> impl Strategy<Value = (Vec<String>, Vec<String>, Vec<Op>)> {
    (
        prop::collection::vec(valid_bucket(), 1..4),
        prop::collection::vec(fs_key(), 1..6),
    )
        .prop_flat_map(|(buckets, keys)| {
            let n_b = buckets.len();
            let n_k = keys.len();
            prop::collection::vec(op_strategy(n_b, n_k), 1..32)
                .prop_map(move |ops| (buckets.clone(), keys.clone(), ops))
        })
}

// ══ the check ═════════════════════════════════════════════════════════════════

proptest! {
    #![proptest_config(ProptestConfig::with_cases(40))]

    /// ShardStore-style reference model: the same random op sequence drives a
    /// tiny in-memory store and the real Index+Store; observables never diverge.
    #[test]
    fn real_store_matches_reference_model(
        (buckets, keys, ops) in scenario_strategy()
    ) {
        // proptest bodies are sync; `?` needs an explicit Result context.
        (|| {
            let (_dir, store, index) = fresh_full();
            let mut model = Model::default();

            for (step, op) in ops.iter().enumerate() {
                // Real first while `model` is still pre-op (shared precon for Put).
                let real_obs =
                    block_on(apply_real(&store, &index, &model, &buckets, &keys, op))?;
                let model_obs = apply_model(&mut model, &buckets, &keys, op);
                prop_assert_eq!(
                    &model_obs,
                    &real_obs,
                    "divergence at step {} after op {:?}",
                    step,
                    op
                );
            }

            assert_final_state(&model, &store, &index)?;
            Ok::<(), TestCaseError>(())
        })()?;
    }
}
