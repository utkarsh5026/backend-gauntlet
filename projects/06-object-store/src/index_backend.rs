//! Index-as-a-service backend — From the field (ungraded).
//!
//! At S3 scale the key→location map is a **separately scaled metadata subsystem**
//! (persistence tier + coherent cache + witness), not a struct in the front-end
//! process. See [`RESEARCH.md` Part 2](../RESEARCH.md).
//!
//! - [`IndexBackend::Local`] delegates to the in-process [`Index`] (default when
//!   `INDEX_URL` is unset).
//! - [`IndexBackend::Remote`] / [`RemoteIndex`] speak HTTP JSON to the
//!   `object-store-index` binary when `INDEX_URL` is set.
//!
//! See `docs/05-how-index-as-a-service-works.md`.

use std::sync::Arc;

use serde::{Deserialize, Serialize};
use url::Url;

use crate::bucket::BucketMetadata;
use crate::error::AppError;
use crate::index::{Index, Listing, NewVersion, Precondition};
use crate::object::{Digest, ETag, ObjectMeta, ObjectRef, ResolvedObject, VersionId};

/// Body for `PUT /v1/{bucket}/keys/{key}` — publish a new live version.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PutRequest {
    pub digest: Digest,
    pub etag: ETag,
    pub size: u64,
    pub content_type: String,
    #[serde(default)]
    pub precondition: PreconditionWire,
}

impl PutRequest {
    pub fn into_parts(self) -> (NewVersion, Precondition) {
        (
            NewVersion {
                digest: self.digest,
                etag: self.etag,
                size: self.size,
                content_type: self.content_type,
            },
            self.precondition.into(),
        )
    }
}

/// Wire form of [`Precondition`] — kept separate so the on-disk index types
/// need not grow serde for HTTP.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum PreconditionWire {
    #[default]
    None,
    IfMatch {
        etag: ETag,
    },
    IfNoneMatchStar,
}

impl From<PreconditionWire> for Precondition {
    fn from(w: PreconditionWire) -> Self {
        match w {
            PreconditionWire::None => Precondition::None,
            PreconditionWire::IfMatch { etag } => Precondition::IfMatch(etag),
            PreconditionWire::IfNoneMatchStar => Precondition::IfNoneMatchStar,
        }
    }
}

impl From<Precondition> for PreconditionWire {
    fn from(p: Precondition) -> Self {
        match p {
            Precondition::None => PreconditionWire::None,
            Precondition::IfMatch(etag) => PreconditionWire::IfMatch { etag },
            Precondition::IfNoneMatchStar => PreconditionWire::IfNoneMatchStar,
        }
    }
}

/// Which version a resolve/delete addresses.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ObjectRefWire {
    Latest,
    Version { id: VersionId },
}

impl From<ObjectRefWire> for ObjectRef {
    fn from(w: ObjectRefWire) -> Self {
        match w {
            ObjectRefWire::Latest => ObjectRef::Latest,
            ObjectRefWire::Version { id } => ObjectRef::Version(id),
        }
    }
}

impl From<ObjectRef> for ObjectRefWire {
    fn from(r: ObjectRef) -> Self {
        match r {
            ObjectRef::Latest => ObjectRefWire::Latest,
            ObjectRef::Version(id) => ObjectRefWire::Version { id },
        }
    }
}

/// Query params for `GET /v1/{bucket}/list`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ListQuery {
    #[serde(default)]
    pub prefix: String,
    pub delimiter: Option<String>,
    pub continuation: Option<String>,
    #[serde(default = "default_max_keys")]
    pub max_keys: usize,
}

fn default_max_keys() -> usize {
    1000
}

/// Wire form of [`Listing`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListingWire {
    pub objects: Vec<ObjectMeta>,
    pub common_prefixes: Vec<String>,
    pub next_continuation_token: Option<String>,
}

impl From<Listing> for ListingWire {
    fn from(l: Listing) -> Self {
        Self {
            objects: l.objects,
            common_prefixes: l.common_prefixes,
            next_continuation_token: l.next_continuation_token,
        }
    }
}

impl From<ListingWire> for Listing {
    fn from(l: ListingWire) -> Self {
        Self {
            objects: l.objects,
            common_prefixes: l.common_prefixes,
            next_continuation_token: l.next_continuation_token,
        }
    }
}

/// Wire form of [`ResolvedObject`] (domain type is not serde'd).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolvedObjectWire {
    pub bucket: String,
    pub key: String,
    pub version_id: VersionId,
    pub digest: Digest,
    pub etag: ETag,
    pub size: u64,
    pub content_type: String,
    pub last_modified: chrono::DateTime<chrono::Utc>,
}

impl From<ResolvedObject> for ResolvedObjectWire {
    fn from(r: ResolvedObject) -> Self {
        Self {
            bucket: r.bucket,
            key: r.key,
            version_id: r.version_id,
            digest: r.digest,
            etag: r.etag,
            size: r.size,
            content_type: r.content_type,
            last_modified: r.last_modified,
        }
    }
}

impl From<ResolvedObjectWire> for ResolvedObject {
    fn from(r: ResolvedObjectWire) -> Self {
        Self {
            bucket: r.bucket,
            key: r.key,
            version_id: r.version_id,
            digest: r.digest,
            etag: r.etag,
            size: r.size,
            content_type: r.content_type,
            last_modified: r.last_modified,
        }
    }
}

/// Body for resolve / delete when the version is not in the path.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectRefBody {
    #[serde(default = "default_latest_ref")]
    pub object_ref: ObjectRefWire,
}

fn default_latest_ref() -> ObjectRefWire {
    ObjectRefWire::Latest
}

/// `POST /v1/gc` response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GcResponse {
    pub reclaimed: u64,
}

/// JSON error body from the index service (mirrors S3 error codes by name).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexErrorBody {
    pub code: String,
    pub message: String,
}

// ── Remote client ───────────────────────────────────────────────────────────

/// HTTP client for the index microservice (`object-store-index`).
#[derive(Debug, Clone)]
pub struct RemoteIndex {
    base_url: String,
    client: reqwest::Client,
}

impl RemoteIndex {
    pub fn new(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            client: reqwest::Client::new(),
        }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    fn parse_base(&self) -> Result<Url, AppError> {
        Url::parse(&self.base_url)
            .map_err(|e| AppError::InvalidRequest(format!("bad INDEX_URL: {e}")))
    }

    fn with_segments(&self, segments: &[&str]) -> Result<Url, AppError> {
        let mut url = self.parse_base()?;
        {
            let mut path = url.path_segments_mut().map_err(|_| {
                AppError::InvalidRequest("INDEX_URL cannot be a base for path joins".into())
            })?;
            // Drop an empty trailing segment from `http://host:port/`.
            path.clear();
            for s in segments {
                path.push(s);
            }
        }
        Ok(url)
    }

    fn bucket_url(&self, bucket: &str) -> Result<Url, AppError> {
        self.with_segments(&["v1", "buckets", bucket])
    }

    fn key_url(&self, bucket: &str, key: &str) -> Result<Url, AppError> {
        let mut url = self.parse_base()?;
        {
            let mut path = url.path_segments_mut().map_err(|_| {
                AppError::InvalidRequest("INDEX_URL cannot be a base for path joins".into())
            })?;
            path.clear();
            path.extend(["v1", "buckets", bucket, "keys"]);
            for segment in key.split('/') {
                path.push(segment);
            }
        }
        Ok(url)
    }

    async fn map_response_error(&self, res: reqwest::Response) -> AppError {
        let status = res.status();
        if let Ok(body) = res.json::<IndexErrorBody>().await {
            return app_error_from_wire(&body.code, body.message);
        }
        AppError::Other(anyhow::anyhow!(
            "index service HTTP {status} (unparseable error body)"
        ))
    }

    /// Send `req`, map transport errors, and require a success status (no body).
    async fn request_empty(&self, req: reqwest::RequestBuilder) -> Result<(), AppError> {
        let res = req.send().await.map_err(|e| AppError::Other(e.into()))?;
        if res.status().is_success() {
            return Ok(());
        }
        Err(self.map_response_error(res).await)
    }

    /// Send `req`, map transport errors, and decode a JSON success body.
    async fn request_json<T: for<'de> Deserialize<'de>>(
        &self,
        req: reqwest::RequestBuilder,
    ) -> Result<T, AppError> {
        let res = req.send().await.map_err(|e| AppError::Other(e.into()))?;
        if res.status().is_success() {
            return res
                .json()
                .await
                .map_err(|e| AppError::Other(anyhow::anyhow!("index JSON decode: {e}")));
        }
        Err(self.map_response_error(res).await)
    }

    pub async fn create_bucket(&self, bucket: &str) -> Result<(), AppError> {
        self.request_empty(self.client.put(self.bucket_url(bucket)?))
            .await
    }

    pub async fn buckets(&self) -> Result<Vec<String>, AppError> {
        let url = self.with_segments(&["v1", "buckets"])?;
        self.request_json(self.client.get(url)).await
    }

    pub async fn ensure_bucket(&self, bucket: &str) -> Result<(), AppError> {
        self.request_empty(self.client.head(self.bucket_url(bucket)?))
            .await
    }

    pub async fn put(
        &self,
        bucket: &str,
        key: &str,
        version: NewVersion,
        pre: Precondition,
    ) -> Result<ObjectMeta, AppError> {
        let url = self.key_url(bucket, key)?;
        let body = PutRequest {
            digest: version.digest,
            etag: version.etag,
            size: version.size,
            content_type: version.content_type,
            precondition: pre.into(),
        };
        self.request_json(self.client.put(url).json(&body)).await
    }

    pub async fn get(&self, bucket: &str, key: &str) -> Result<Option<ObjectMeta>, AppError> {
        let url = self.key_url(bucket, key)?;
        self.request_json(self.client.get(url)).await
    }

    pub async fn resolve(
        &self,
        bucket: &str,
        key: &str,
        object_ref: ObjectRef,
    ) -> Result<ResolvedObject, AppError> {
        // Separate prefix from `/keys/{*key}` — axum cannot nest under a wildcard.
        let mut url = self.parse_base()?;
        {
            let mut path = url.path_segments_mut().map_err(|_| {
                AppError::InvalidRequest("INDEX_URL cannot be a base for path joins".into())
            })?;
            path.clear();
            path.extend(["v1", "buckets", bucket, "resolve"]);
            for segment in key.split('/') {
                path.push(segment);
            }
        }
        let body = ObjectRefBody {
            object_ref: object_ref.into(),
        };
        let wire: ResolvedObjectWire = self.request_json(self.client.post(url).json(&body)).await?;
        Ok(wire.into())
    }

    pub async fn delete(
        &self,
        bucket: &str,
        key: &str,
        object_ref: ObjectRef,
    ) -> Result<(), AppError> {
        let url = self.key_url(bucket, key)?;
        let body = ObjectRefBody {
            object_ref: object_ref.into(),
        };
        self.request_empty(self.client.delete(url).json(&body))
            .await
    }

    pub async fn list(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        continuation: Option<&str>,
        max_keys: usize,
    ) -> Result<Listing, AppError> {
        let mut url = self.with_segments(&["v1", "buckets", bucket, "list"])?;
        {
            let mut q = url.query_pairs_mut();
            q.append_pair("prefix", prefix);
            q.append_pair("max_keys", &max_keys.to_string());
            if let Some(d) = delimiter {
                q.append_pair("delimiter", d);
            }
            if let Some(c) = continuation {
                q.append_pair("continuation", c);
            }
        }
        let wire: ListingWire = self.request_json(self.client.get(url)).await?;
        Ok(wire.into())
    }

    pub async fn index_entries(&self, bucket: &str) -> Result<Vec<ObjectMeta>, AppError> {
        let url = self.with_segments(&["v1", "buckets", bucket, "entries"])?;
        self.request_json(self.client.get(url)).await
    }

    pub async fn gc(&self) -> Result<u64, AppError> {
        let url = self.with_segments(&["v1", "gc"])?;
        let body: GcResponse = self.request_json(self.client.post(url)).await?;
        Ok(body.reclaimed)
    }

    pub async fn load_bucket_metadata(&self, bucket: &str) -> Result<BucketMetadata, AppError> {
        let url = self.with_segments(&["v1", "buckets", bucket, "metadata"])?;
        self.request_json(self.client.get(url)).await
    }

    pub async fn store_bucket_metadata(
        &self,
        bucket: &str,
        meta: &BucketMetadata,
    ) -> Result<(), AppError> {
        let url = self.with_segments(&["v1", "buckets", bucket, "metadata"])?;
        self.request_empty(self.client.put(url).json(meta)).await
    }
}

fn app_error_from_wire(code: &str, message: String) -> AppError {
    match code {
        "NoSuchBucket" => AppError::NoSuchBucket,
        "NoSuchKey" => AppError::NoSuchKey,
        "NoSuchUpload" => AppError::NoSuchUpload,
        "BucketAlreadyExists" => AppError::BucketAlreadyExists,
        "InvalidRequest" => AppError::InvalidRequest(message),
        "EntityTooLarge" => AppError::EntityTooLarge,
        "PreconditionFailed" => AppError::PreconditionFailed,
        _ => AppError::Other(anyhow::anyhow!("index service: {code}: {message}")),
    }
}

/// Process-local or RPC-backed index. [`AppState`] holds `Arc<IndexBackend>`;
/// set `INDEX_URL` to pick [`Self::Remote`].
#[derive(Clone)]
pub enum IndexBackend {
    Local(Arc<Index>),
    Remote(RemoteIndex),
}

impl IndexBackend {
    pub fn local(index: Arc<Index>) -> Self {
        Self::Local(index)
    }

    pub fn remote(base_url: impl Into<String>) -> Self {
        Self::Remote(RemoteIndex::new(base_url))
    }

    pub async fn create_bucket(&self, bucket: &str) -> Result<(), AppError> {
        match self {
            Self::Local(i) => i.create_bucket(bucket).await,
            Self::Remote(r) => r.create_bucket(bucket).await,
        }
    }

    pub async fn buckets(&self) -> Result<Vec<String>, AppError> {
        match self {
            Self::Local(i) => i.buckets().await,
            Self::Remote(r) => r.buckets().await,
        }
    }

    /// Network-friendly ensure: existence check only (no [`std::path::PathBuf`]).
    pub async fn ensure_bucket(&self, bucket: &str) -> Result<(), AppError> {
        match self {
            Self::Local(i) => {
                i.ensure_bucket(bucket).await?;
                Ok(())
            }
            Self::Remote(r) => r.ensure_bucket(bucket).await,
        }
    }

    pub async fn put(
        &self,
        bucket: &str,
        key: &str,
        version: NewVersion,
        pre: Precondition,
    ) -> Result<ObjectMeta, AppError> {
        match self {
            Self::Local(i) => i.put(bucket, key, version, pre).await,
            Self::Remote(r) => r.put(bucket, key, version, pre).await,
        }
    }

    pub async fn get(&self, bucket: &str, key: &str) -> Result<Option<ObjectMeta>, AppError> {
        match self {
            Self::Local(i) => i.get(bucket, key).await,
            Self::Remote(r) => r.get(bucket, key).await,
        }
    }

    pub async fn resolve(
        &self,
        bucket: &str,
        key: &str,
        object_ref: ObjectRef,
    ) -> Result<ResolvedObject, AppError> {
        match self {
            Self::Local(i) => i.resolve(bucket, key, object_ref).await,
            Self::Remote(r) => r.resolve(bucket, key, object_ref).await,
        }
    }

    pub async fn delete(
        &self,
        bucket: &str,
        key: &str,
        object_ref: ObjectRef,
    ) -> Result<(), AppError> {
        match self {
            Self::Local(i) => i.delete(bucket, key, object_ref).await,
            Self::Remote(r) => r.delete(bucket, key, object_ref).await,
        }
    }

    pub async fn list(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        continuation: Option<&str>,
        max_keys: usize,
    ) -> Result<Listing, AppError> {
        match self {
            Self::Local(i) => {
                i.list(bucket, prefix, delimiter, continuation, max_keys)
                    .await
            }
            Self::Remote(r) => {
                r.list(bucket, prefix, delimiter, continuation, max_keys)
                    .await
            }
        }
    }

    pub async fn index_entries(&self, bucket: &str) -> Result<Vec<ObjectMeta>, AppError> {
        match self {
            Self::Local(i) => i.index_entries(bucket).await,
            Self::Remote(r) => r.index_entries(bucket).await,
        }
    }

    pub async fn gc(&self) -> Result<u64, AppError> {
        match self {
            Self::Local(i) => i.gc().await,
            Self::Remote(r) => r.gc().await,
        }
    }

    /// Load `index/<bucket>/metadata.json` (locally or via the index service).
    pub async fn load_bucket_metadata(&self, bucket: &str) -> Result<BucketMetadata, AppError> {
        match self {
            Self::Local(i) => {
                let dir = i.ensure_bucket(bucket).await?;
                BucketMetadata::load(&dir).await
            }
            Self::Remote(r) => r.load_bucket_metadata(bucket).await,
        }
    }

    /// Persist bucket metadata (lifecycle policy, etc.).
    pub async fn store_bucket_metadata(
        &self,
        bucket: &str,
        meta: &BucketMetadata,
    ) -> Result<(), AppError> {
        match self {
            Self::Local(i) => {
                let dir = i.ensure_bucket(bucket).await?;
                meta.store(&dir).await
            }
            Self::Remote(r) => r.store_bucket_metadata(bucket, meta).await,
        }
    }
}
