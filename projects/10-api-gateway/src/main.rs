//! API gateway / L7 reverse proxy — binary entrypoint.
//!
//! The plumbing (config, the pooled upstream client, the route table, the axum
//! server, graceful shutdown, `/metrics`) is wired for you. The learning lives in
//! the modules marked `TODO(Vx)` — see `lib.rs` and `SPEC.md`. This binary is a
//! thin shell over the `api_gateway` library crate so the proxy path is reachable
//! from `tests/`.
//!
//! Scaffold state: this compiles and serves. `GET /healthz`, `GET /metrics`, and
//! `GET /admin/routes` work; the first request that must actually be *proxied*
//! hits a `todo!()` (route match → backend pick → forward) and panics — that panic
//! message is your worklist.

use std::sync::Arc;

use common_config::TimeUnit;
use metrics_exporter_prometheus::PrometheusBuilder;
use tracing::info;

use api_gateway::config::GatewayConfig;
use api_gateway::router::Router;
use api_gateway::{
    routes, upstream_client, AppState, DEFAULT_CONNECT_TIMEOUT_MS, DEFAULT_MAX_BODY_BYTES,
    DEFAULT_PORT, DEFAULT_REQUEST_TIMEOUT_MS,
};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,api_gateway=debug");
    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    // `..._MS` names the unit a bare number is read in; an operator can still
    // override it in the value itself (`REQUEST_TIMEOUT_MS=2s`).
    let connect_timeout = common_config::duration_or(
        "UPSTREAM_CONNECT_TIMEOUT_MS",
        TimeUnit::Millis,
        DEFAULT_CONNECT_TIMEOUT_MS,
    );
    let request_timeout = common_config::duration_or(
        "REQUEST_TIMEOUT_MS",
        TimeUnit::Millis,
        DEFAULT_REQUEST_TIMEOUT_MS,
    );
    let max_body_bytes: u64 = common_config::parse_or("MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES);

    // Route table: an explicit JSON file (CONFIG_PATH) or a built-in catch-all over
    // UPSTREAM_BACKENDS so `cargo run` + docker-compose work with zero config files.
    let config = match common_config::or_default("CONFIG_PATH", "") {
        path if !path.is_empty() => GatewayConfig::load(&path)?,
        _ => {
            let backends = common_config::or_default("UPSTREAM_BACKENDS", "127.0.0.1:9010")
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect::<Vec<_>>();
            GatewayConfig::demo(backends)
        }
    };
    let router = Arc::new(Router::build(&config)?);
    info!(routes = config.routes.len(), "route table built");

    let client = upstream_client(connect_timeout);
    let prometheus = PrometheusBuilder::new().install_recorder()?;

    // TODO(V4): spawn the active health checker once you build it, e.g.
    //   let probe = common_config::duration_or("HEALTH_PROBE_MS", TimeUnit::Millis, 2_000);
    //   tokio::spawn(health::HealthChecker::new(router.clone(), client.clone(), probe).run());
    //
    // TODO(mTLS): when TLS_CERT/TLS_KEY are set, build the rustls server config
    //   (`tls::server_config`) and serve over a `tls::acceptor` instead of plain TCP.

    let state = AppState {
        client,
        router,
        prometheus,
        request_timeout,
        max_body_bytes,
    };
    let app = routes::router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(%addr, "api-gateway listening — GET /admin/routes for the table; every other path is proxied");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Waits for Ctrl-C / SIGTERM so axum can drain in-flight requests.
///
/// TODO(graceful shutdown): stop accepting new connections and drain in-flight
/// *proxied* requests within a deadline before returning, so a client mid-download
/// gets a complete response rather than a truncated one.
async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
