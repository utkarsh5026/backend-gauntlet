//! URL shortener — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, DB/Redis connections, router, graceful
//! shutdown, the click-ingestion background task) is wired up for you. The parts
//! marked `TODO` in the other modules are where the real learning lives — see
//! SPEC.md (V1 ids, V2 cache, V3 ingestion, plus the security checklist).

mod auth;
mod cache;
mod error;
mod id_gen;
mod ingest;
mod metrics;
mod ratelimit;
mod routes;
mod url_validate;

#[cfg(test)]
mod test_support;

use std::collections::HashSet;
use std::sync::Arc;

use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;
use tracing::info;

use cache::Cache;
use id_gen::IdGenerator;
use ingest::{ClickIngestor, ClickSink};

const DEFAULT_PORT: u16 = 8080;

/// Shared application state, cloned into every request handler.
/// Everything in here is cheap to clone (pools/managers are `Arc` inside, the
/// id generator and key set we wrap in `Arc` ourselves).
#[derive(Clone)]
pub struct AppState {
    pub db: PgPool,
    pub cache: Cache,
    pub ids: Arc<IdGenerator>,
    pub clicks: ClickSink,
    pub api_keys: Arc<HashSet<String>>,
    pub base_url: Arc<str>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,url_shortener=debug");
    let metrics_handle = metrics::install();

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let database_url = common_config::require("DATABASE_URL")?;
    let redis_url = common_config::require("REDIS_URL")?;
    let node_id: u16 = common_config::parse_or("NODE_ID", 1);
    let base_url = common_config::or_default("PUBLIC_BASE_URL", format!("http://localhost:{port}"));
    let api_keys: HashSet<String> = common_config::or_default("API_KEYS", "")
        .split(',')
        .filter(|s| !s.is_empty())
        .map(|s| s.trim().to_string())
        .collect();

    let db = PgPoolOptions::new()
        .max_connections(20)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    let redis_client = redis::Client::open(redis_url)?;
    let conn_manager = redis_client.get_connection_manager().await?;
    let cache = Cache::new(conn_manager);
    info!("connected to redis");

    let (ingestor, clicks) = ClickIngestor::new(db.clone());
    tokio::spawn(ingestor.run());

    let state = AppState {
        db,
        cache,
        ids: Arc::new(IdGenerator::new(node_id)),
        clicks,
        api_keys: Arc::new(api_keys),
        base_url: Arc::from(base_url),
    };

    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight requests.
/// TODO(SPEC): on shutdown, also flush the click buffer before exiting.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
