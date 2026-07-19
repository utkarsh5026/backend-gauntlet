//! S3-compatible object store — library surface.
//!
//! `main.rs` is a thin binary over this crate. The modules and [`AppState`] live
//! here (rather than in `main.rs`) so that integration tests in `tests/` can
//! build the router against a throwaway data dir and drive it through the real
//! HTTP surface — the same code path a client hits over the wire.
//!
//! There is no external dependency: the filesystem IS the store. Scaffold state:
//! this compiles and serves. `GET /healthz` works; the first real PUT/GET/list
//! hits a `todo!()` and panics — that panic message is your worklist.

pub mod bucket;
pub mod durable;
pub mod error;
pub mod index;
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

use index::Index;
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
    pub index: Arc<Index>,
    pub multipart: Arc<Multipart>,
    pub lifecycle: Arc<Lifecycle>,
    pub max_object_size: u64,
}

impl AppState {
    pub fn open(data_dir: impl AsRef<Path>, max_object_size: u64) -> anyhow::Result<Self> {
        let data_dir = data_dir.as_ref();
        let store = Store::open(data_dir)?;
        let index = Index::open(data_dir, store.clone())?;
        let multipart = Multipart::open(data_dir, store.clone(), index.clone())?;
        let lifecycle = Lifecycle::new(index.clone(), store.clone());
        Ok(Self {
            store,
            index,
            multipart,
            lifecycle,
            max_object_size,
        })
    }
}
