//! Ledger / payments core (Stripe-lite) — entrypoint and wiring.
//!
//! The plumbing (config, telemetry, the Postgres pool + Redis connection, the router
//! and `/metrics`, the webhook dispatcher, graceful shutdown) is wired up for you. The
//! learning lives in the modules marked `TODO(Vx)`: the double-entry posting engine
//! (V1, `ledger.rs`), the concurrency-safe transfer under `SERIALIZABLE` (V2,
//! `isolation.rs`), idempotency keys (V3, `idempotency.rs`), and signed webhook
//! delivery via a transactional outbox (V4, `webhooks.rs`). See SPEC.md.
//!
//! Scaffold state: this compiles and serves the API. `POST /accounts` and
//! `POST /transfers` `todo!()`-panic on their first call, and turning on
//! `RUN_DISPATCHER` makes the webhook loop panic on its first outbox claim — those
//! panic messages are your worklist.

mod error;
mod idempotency;
mod isolation;
mod ledger;
mod metrics;
mod money;
mod routes;
mod webhooks;

use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::info;

use idempotency::IdempotencyStore;
use isolation::TransferConfig;
use ledger::Ledger;
use webhooks::WebhookConfig;

const DEFAULT_PORT: u16 = 8080;
const DEFAULT_WEBHOOK_ENDPOINT: &str = "http://localhost:9000/webhooks";

/// Shared application state, cloned into every request handler. The heavy handles are
/// behind `Arc`, so cloning is cheap; `TransferConfig` is small and `Clone`.
#[derive(Clone)]
pub struct AppState {
    pub ledger: Arc<Ledger>,
    pub idempotency: Arc<IdempotencyStore>,
    pub transfer_cfg: TransferConfig,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,ledger_payments_core=debug,sqlx=warn");

    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let database_url = common_config::require("DATABASE_URL")?;
    let redis_url = common_config::require("REDIS_URL")?;
    let db_max_connections: u32 = common_config::parse_or("DB_MAX_CONNECTIONS", 20);
    let idempotency_ttl: i64 = common_config::parse_or("IDEMPOTENCY_TTL_SECS", 86_400);
    let webhook_endpoint =
        common_config::or_default("WEBHOOK_ENDPOINT_URL", DEFAULT_WEBHOOK_ENDPOINT);

    // Postgres: the transactional source of truth for the whole ledger.
    let pool = PgPoolOptions::new()
        .max_connections(db_max_connections)
        .connect(&database_url)
        .await?;
    info!("connected to postgres");

    // Redis: the idempotency response cache (V3). The ConnectionManager reconnects
    // under the hood, so a blip degrades to a Postgres read rather than an error.
    let redis_client = redis::Client::open(redis_url)?;
    let redis_conn = redis_client.get_connection_manager().await?;
    info!("connected to redis");

    let ledger = Ledger::new(pool.clone());
    let idempotency = IdempotencyStore::new(pool.clone(), redis_conn, idempotency_ttl);

    let transfer_cfg = TransferConfig {
        max_retries: common_config::parse_or("MAX_SERIALIZATION_RETRIES", 5),
        max_amount: common_config::parse_or("MAX_TRANSFER_MINOR", 100_000_000),
        webhook_endpoint: Some(webhook_endpoint.clone()),
    };

    // Prometheus recorder + a handle to render `/metrics`.
    let metrics_handle = metrics::install();

    // Graceful shutdown is broadcast to every background task via this watch.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // The webhook dispatcher runs only when asked, so the bare scaffold serves the
    // API cleanly. (Its first outbox claim is a V4 `todo!()` — flip RUN_DISPATCHER=true
    // once V4 works.)
    let mut tasks = Vec::new();
    if common_config::parse_or("RUN_DISPATCHER", false) {
        let cfg = WebhookConfig {
            signing_secret: common_config::require("WEBHOOK_SIGNING_SECRET")?,
            endpoint_url: webhook_endpoint.clone(),
            max_attempts: common_config::parse_or("WEBHOOK_MAX_ATTEMPTS", 8_i32),
            dispatch_interval: Duration::from_millis(common_config::parse_or(
                "WEBHOOK_DISPATCH_INTERVAL_MS",
                1000_u64,
            )),
            dispatch_batch: common_config::parse_or("WEBHOOK_DISPATCH_BATCH", 50_i64),
        };
        tasks.push(tokio::spawn(webhooks::dispatch_loop(
            pool.clone(),
            cfg,
            shutdown_rx.clone(),
        )));
        info!("webhook dispatcher started");
    } else {
        info!("webhook dispatcher disabled (RUN_DISPATCHER=false): API only");
    }

    let state = AppState {
        ledger,
        idempotency,
        transfer_cfg,
    };
    let app = routes::router(state).merge(routes::metrics_router(metrics_handle));

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "listening (POST /accounts, POST /transfers)");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // Tell background tasks to drain, then wait for them to finish.
    let _ = shutdown_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so we can drain in-flight work.
///
/// TODO(SPEC): on shutdown, let in-flight transfers finish and the webhook dispatcher
/// complete its current batch before exiting — never abandon a half-delivered event.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
