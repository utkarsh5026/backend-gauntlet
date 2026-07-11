//! S3-compatible object store — binary entrypoint.
//!
//! The plumbing (config, the on-disk store layout, the axum router, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)` — see `lib.rs` and `SPEC.md`. This binary is a thin shell over the
//! `object_store` library crate so the router is reachable from `tests/`.

use tracing::info;

use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};

const DEFAULT_PORT: u16 = 9000;
const DEFAULT_DATA_DIR: &str = "./data";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,object_store=debug");

    // Install the process-global Prometheus recorder once, right after telemetry.
    // Until this runs the `metrics::*` call sites in the modules are no-ops.
    let metrics_handle = object_store::metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);
    let max_object_size: u64 = common_config::parse_or("MAX_OBJECT_SIZE", DEFAULT_MAX_OBJECT_SIZE);

    let state = AppState::open(&data_dir, max_object_size)?;
    info!(%data_dir, max_object_size, "object store opened");

    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

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
