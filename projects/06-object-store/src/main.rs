//! S3-compatible object store — entrypoint and wiring.
//!
//! The plumbing (config, the on-disk store layout, the axum router, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)`: the content-addressed durable blob store (V1, `store.rs`),
//! streaming bodies with bounded memory (V2, `streaming.rs`), the bucket/key
//! index + prefix listing + GC (V3, `index.rs`), and multipart upload + the S3
//! ETag (V4, `multipart.rs`). See SPEC.md.
//!
//! There is no external dependency: the filesystem IS the store. Scaffold state:
//! this compiles and serves. `GET /healthz` works; the first real PUT/GET/list
//! hits a `todo!()` and panics — that panic message is your worklist.

mod error;
mod index;
mod multipart;
mod object;
mod routes;
mod store;
mod streaming;

use std::sync::Arc;

use tracing::info;

use index::Index;
use multipart::Multipart;
use store::Store;

const DEFAULT_PORT: u16 = 9000;
const DEFAULT_DATA_DIR: &str = "./data";
/// S3's single-PUT ceiling (5 GiB). The real enforcement is in the V2 stream
/// loop; axum's own 2 MB body limit is disabled in the router.
const DEFAULT_MAX_OBJECT_SIZE: u64 = 5 * 1024 * 1024 * 1024;

/// Shared application state, cloned into every request handler. Each vertical's
/// type is behind an `Arc`, so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    pub index: Arc<Index>,
    pub multipart: Arc<Multipart>,
    /// Per-object / per-part size cap, enforced while streaming (V2).
    pub max_object_size: u64,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,object_store=debug");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);
    let max_object_size: u64 = common_config::parse_or("MAX_OBJECT_SIZE", DEFAULT_MAX_OBJECT_SIZE);

    let store = Store::open(&data_dir)?;
    let index = Index::open(&data_dir, store.clone())?;
    let multipart = Multipart::open(&data_dir, store.clone(), index.clone())?;
    info!(%data_dir, max_object_size, "object store opened");

    let state = AppState {
        store,
        index,
        multipart,
        max_object_size,
    };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (S3 path-style; PUT /{{bucket}}/{{key}} to store an object)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight streams.
///
/// TODO(SPEC): on shutdown, let in-flight uploads/downloads finish (or fail
/// cleanly so their temp files are reclaimed) — never abort mid-stream and leave
/// a partial temp blob behind.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
