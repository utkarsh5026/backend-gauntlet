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
mod routes;

use std::collections::HashSet;
use std::sync::Arc;

use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;
use tokio::sync::mpsc;
use tracing::info;

use cache::Cache;
use id_gen::IdGenerator;

/// Shared application state, cloned into every request handler.
/// Everything in here is cheap to clone (pools/managers are `Arc` inside, the
/// id generator and key set we wrap in `Arc` ourselves).
#[derive(Clone)]
pub struct AppState {
    pub db: PgPool,
    pub cache: Cache,
    pub ids: Arc<IdGenerator>,
    pub clicks: mpsc::Sender<ClickEvent>,
    pub api_keys: Arc<HashSet<String>>,
}

/// A single recorded click, handed off to the background ingestion task so the
/// redirect hot path never blocks on a DB write (V3).
#[derive(Debug, Clone)]
pub struct ClickEvent {
    pub link_id: i64,
    pub referer: Option<String>,
    pub user_agent: Option<String>,
    pub ip_hash: Option<String>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,url_shortener=debug");

    // --- config ---
    let port: u16 = common_config::parse_or("PORT", 8080);
    let database_url = common_config::require("DATABASE_URL")?;
    let redis_url = common_config::require("REDIS_URL")?;
    let node_id: u16 = common_config::parse_or("NODE_ID", 1);
    let api_keys: HashSet<String> = common_config::or_default("API_KEYS", "")
        .split(',')
        .filter(|s| !s.is_empty())
        .map(|s| s.trim().to_string())
        .collect();

    // --- connections ---
    let db = PgPoolOptions::new()
        .max_connections(20)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    let redis_client = redis::Client::open(redis_url)?;
    let conn_manager = redis_client.get_connection_manager().await?;
    let cache = Cache::new(conn_manager);
    info!("connected to redis");

    // --- background click ingestion (V3) ---
    // Bounded channel == backpressure. Decide in SPEC.md what happens when full.
    let (clicks_tx, clicks_rx) = mpsc::channel::<ClickEvent>(10_000);
    tokio::spawn(ingest::run(db.clone(), clicks_rx));

    let state = AppState {
        db,
        cache,
        ids: Arc::new(IdGenerator::new(node_id)),
        clicks: clicks_tx,
        api_keys: Arc::new(api_keys),
    };

    let app = routes::router(state);

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

/// Background task that batches click events and writes them to Postgres.
mod ingest {
    use super::ClickEvent;
    use sqlx::PgPool;
    use std::time::Duration;
    use tokio::sync::mpsc::Receiver;
    use tracing::{debug, warn};

    /// Drains the channel, batching by size or time, and bulk-inserts.
    pub async fn run(_db: PgPool, mut rx: Receiver<ClickEvent>) {
        const MAX_BATCH: usize = 500;
        const FLUSH_EVERY: Duration = Duration::from_millis(500);

        let mut buf: Vec<ClickEvent> = Vec::with_capacity(MAX_BATCH);
        let mut ticker = tokio::time::interval(FLUSH_EVERY);

        loop {
            tokio::select! {
                maybe = rx.recv() => match maybe {
                    Some(ev) => {
                        buf.push(ev);
                        if buf.len() >= MAX_BATCH {
                            flush(&_db, &mut buf).await;
                        }
                    }
                    None => { // channel closed: final flush and exit
                        flush(&_db, &mut buf).await;
                        break;
                    }
                },
                _ = ticker.tick() => flush(&_db, &mut buf).await,
            }
        }
        debug!("click ingestor stopped");
    }

    async fn flush(_db: &PgPool, buf: &mut Vec<ClickEvent>) {
        if buf.is_empty() {
            return;
        }
        let n = buf.len();
        // TODO(V3): bulk-insert `buf` into `click_events` in ONE statement
        // (look up sqlx `QueryBuilder::push_values` / UNNEST). Handle errors
        // without losing the whole batch silently — decide your policy.
        warn!(
            count = n,
            "TODO: implement batched click insert (events dropped for now)"
        );
        buf.clear();
    }
}
