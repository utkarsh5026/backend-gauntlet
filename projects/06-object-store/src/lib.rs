//! S3-compatible object store — library surface.
//!
//! `main.rs` is a thin binary over this crate. The modules and [`AppState`] live
//! here (rather than in `main.rs`) so that integration tests in `tests/` can
//! build the router against a throwaway data dir and drive it through the real
//! HTTP surface — the same code path a client hits over the wire.
//!
//! Index mode: unset / empty `INDEX_URL` → in-process [`Index`] via
//! [`IndexBackend::Local`]. Set `INDEX_URL=http://127.0.0.1:9106` →
//! [`IndexBackend::Remote`] talking to `object-store-index` (shared `DATA_DIR`).

pub mod auth;
pub mod bucket;
pub mod durable;
pub mod error;
pub mod index;
pub mod index_backend;
pub mod index_server;
pub mod lifecycle;
pub mod metrics;
pub mod multipart;
pub mod naming;
pub mod object;
pub mod routes;
pub mod s3_xml;
pub mod store;
pub mod streaming;

use std::path::Path;
use std::sync::Arc;

use auth::AuthConfig;
use index::Index;
use index_backend::IndexBackend;
use lifecycle::Lifecycle;
use multipart::Multipart;
use store::Store;

/// S3's single-PUT ceiling (5 GiB). The real enforcement is in the V2 stream
/// loop; axum's own 2 MB body limit is disabled in the router.
pub const DEFAULT_MAX_OBJECT_SIZE: u64 = 5 * 1024 * 1024 * 1024;

/// Shared application state, cloned into every request handler. Each vertical's
/// type is behind an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    pub index: Arc<IndexBackend>,
    pub multipart: Arc<Multipart>,
    pub lifecycle: Arc<Lifecycle>,
    pub max_object_size: u64,
    pub auth: Option<AuthConfig>,
}

impl AppState {
    /// Open the store stack. Index placement:
    /// - `INDEX_URL` empty/unset → local [`Index`] under `data_dir`
    /// - `INDEX_URL` set → [`RemoteIndex`](index_backend::RemoteIndex); blobs
    ///   still live under this process's `data_dir` (`objects/`)
    ///
    /// Auth starts as `None` (open API). Call [`Self::with_auth`] from `main`
    /// after [`AuthConfig::from_env_optional`] so unit/integration tests are not
    /// gated by a developer's shell `SECRET_ACCESS_KEY`.
    pub fn open(data_dir: impl AsRef<Path>, max_object_size: u64) -> anyhow::Result<Self> {
        let data_dir = data_dir.as_ref();
        let store = Store::open(data_dir)?;
        let index = Arc::new(Self::open_index(data_dir, store.clone())?);
        let multipart = Multipart::open(data_dir, store.clone(), index.clone())?;
        let lifecycle = Lifecycle::new(index.clone(), store.clone());
        Ok(Self {
            store,
            index,
            multipart,
            lifecycle,
            max_object_size,
            auth: None,
        })
    }

    /// Install (or clear) presigned-URL auth without reopening the store.
    pub fn with_auth(mut self, auth: Option<AuthConfig>) -> Self {
        self.auth = auth;
        self
    }

    fn open_index(data_dir: &Path, store: Arc<Store>) -> anyhow::Result<IndexBackend> {
        let index_url = std::env::var("INDEX_URL").unwrap_or_default();
        if index_url.is_empty() {
            Ok(IndexBackend::local(Index::open(data_dir, store)?))
        } else {
            tracing::info!(%index_url, "using remote index service");
            Ok(IndexBackend::remote(index_url))
        }
    }
}
