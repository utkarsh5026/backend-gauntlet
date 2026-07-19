//! Index microservice binary — From the field (ungraded).
//!
//! Owns the on-disk `(bucket, key) → blob` map under `DATA_DIR/index`. The S3
//! front-end (`object-store` default bin) keeps blobs in `Store` and, once you
//! adopt the lab, calls this process over HTTP (`INDEX_URL`).
//!
//! Speaks the internal `/v1` JSON API consumed by
//! [`object_store::index_backend::RemoteIndex`]. Wire `AppState` onto
//! `IndexBackend` + `INDEX_URL` to use it from the S3 front-end — see
//! `docs/05-how-index-as-a-service-works.md`.

use tracing::info;

use object_store::index::Index;
use object_store::index_server::{self, IndexServiceState};
use object_store::store::Store;

const DEFAULT_INDEX_PORT: u16 = 9106;
const DEFAULT_DATA_DIR: &str = "./data";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,object_store=debug");

    let port: u16 = common_config::parse_or("INDEX_PORT", DEFAULT_INDEX_PORT);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);

    let store = Store::open(&data_dir)?;
    let index = Index::open(&data_dir, store)?;
    info!(%data_dir, "index service opened (metadata only; blobs stay on the front-end)");

    let state = IndexServiceState { index };
    let app = index_server::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "index service listening (internal /v1 API, not S3 path-style)");

    axum::serve(listener, app).await?;
    Ok(())
}
