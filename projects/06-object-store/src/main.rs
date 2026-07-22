//! S3-compatible object store — binary entrypoint.
//!
//! The plumbing (config, the on-disk store layout, the axum router, graceful
//! shutdown) is wired up for you. The learning lives in the modules marked
//! `TODO(Vx)` — see `lib.rs` and `SPEC.md`. This binary is a thin shell over the
//! `object_store` library crate so the router is reachable from `tests/`.

use std::sync::Arc;
use std::time::Duration;

use tracing::{info, warn};

use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};

const DEFAULT_PORT: u16 = 9000;
const DEFAULT_DATA_DIR: &str = "./data";

const DEFAULT_SHUTDOWN_GRACE_SECS: u64 = 30;
const DEFAULT_LIFECYCLE_SCAN_INTERVAL_SECS: u64 = 60;
const DEFAULT_SCRUB_RESCAN_INTERVAL_SECS: u64 = 300;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,object_store=debug");

    let shutdown_grace = Duration::from_secs(common_config::parse_or(
        "SHUTDOWN_GRACE_SECS",
        DEFAULT_SHUTDOWN_GRACE_SECS,
    ));

    // Install the process-global Prometheus recorder once, right after telemetry.
    // Until this runs the `metrics::*` call sites in the modules are no-ops.
    let metrics_handle = object_store::metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let data_dir = common_config::or_default("DATA_DIR", DEFAULT_DATA_DIR);
    let max_object_size: u64 = common_config::parse_or("MAX_OBJECT_SIZE", DEFAULT_MAX_OBJECT_SIZE);

    let layout = object_store::store::BlobLayoutKind::from_env();
    let state = AppState::open_with_layout(&data_dir, max_object_size, layout)?
        .with_auth(object_store::auth::AuthConfig::from_env_optional())
        .with_cdc(object_store::cdc::CdcConfig::from_env());
    if state.auth.is_some() {
        info!("object auth enabled (presigned URLs + access credentials)");
    } else {
        warn!("object auth disabled — routes are open; set SECRET_ACCESS_KEY to gate them");
    }
    if state.cdc.enabled {
        info!(
            min_chunk = state.cdc.min_size,
            avg_chunk = state.cdc.avg_size,
            max_chunk = state.cdc.max_size,
            min_object = state.cdc.min_object_size,
            "CDC chunk-level dedup enabled (scaffold — PUT path is still todo!())"
        );
    } else {
        info!("CDC chunk-level dedup disabled — whole-object CAS on PUT");
    }
    match state.store.layout_kind() {
        object_store::store::BlobLayoutKind::FileCas => {
            info!("blob write policy: file_cas (all commits under objects/)");
        }
        object_store::store::BlobLayoutKind::Haystack => {
            info!("blob write policy: haystack (small → volumes/, oversized → objects/)");
        }
        object_store::store::BlobLayoutKind::Hybrid => {
            info!("blob write policy: hybrid (small → volumes/, oversized → objects/)");
        }
    }
    info!(%data_dir, max_object_size, "object store opened");

    let lifecycle_scan_interval = Duration::from_secs(common_config::parse_or(
        "LIFECYCLE_SCAN_INTERVAL_SECS",
        DEFAULT_LIFECYCLE_SCAN_INTERVAL_SECS,
    ));
    // Spawn the *same* engine the read path uses (transparent tiered GETs), so
    // there is one `Lifecycle` over the data dir. Keep the JoinHandle for the
    // process lifetime — dropping it aborts the sweeper.
    let _lifecycle = state.lifecycle.clone().spawn(lifecycle_scan_interval);
    info!(?lifecycle_scan_interval, "lifecycle sweeper started");

    let scrub_rescan_interval = Duration::from_secs(common_config::parse_or(
        "SCRUB_RESCAN_INTERVAL_SECS",
        DEFAULT_SCRUB_RESCAN_INTERVAL_SECS,
    ));
    let _scrubber = state.store.clone().spawn_scrubber(scrub_rescan_interval);
    info!(?scrub_rescan_interval, "blob scrubber started");

    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (S3 path-style; PUT /{{bucket}}/{{key}} to store an object)");

    let signal_fired = Arc::new(tokio::sync::Notify::new());

    let server =
        axum::serve(listener, app).with_graceful_shutdown(shutdown_signal(signal_fired.clone()));

    tokio::select! {
        res = server => {
            res?;
            info!("all in-flight requests drained cleanly");
        }

        // The grace deadline expired first. The timer is armed *inside* this
        // future, only after the beacon fires, so `shutdown_grace` counts from
        // the signal — not from boot.
        _ = async {
            signal_fired.notified().await;
            tokio::time::sleep(shutdown_grace).await;
        } => {
            warn!(grace = ?shutdown_grace, "grace expired — forcing shutdown");
            // TODO(SPEC): read the in-flight gauge and log how many streams are
            // being abandoned instead of a blind "forcing".
            // TODO(SPEC): ensure temp-blob guards' Drop reclaims any partial
            // staging file as their handler futures are cancelled here.
        }
    }

    Ok(())
}

async fn shutdown_signal(signal_fired: Arc<tokio::sync::Notify>) {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        use tokio::signal::unix::{signal, SignalKind};
        signal(SignalKind::terminate())
            .expect("failed to install term signal handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    info!("shutdown signal received; draining");
    signal_fired.notify_one();
}
