//! API gateway / L7 reverse proxy — entrypoint and wiring.
//!
//! The plumbing (config, the pooled upstream client, the route table, the axum
//! server, graceful shutdown, `/metrics`) is wired for you. The learning lives in
//! the modules marked `TODO(Vx)`: the streaming forwarding core (V1, `proxy.rs`),
//! the routing engine (V2, `router.rs`), the load balancer (V3, `balancer.rs`),
//! and health checking + circuit breaking (V4, `health.rs`). See SPEC.md.
//!
//! Scaffold state: this compiles and serves. `GET /healthz`, `GET /metrics`, and
//! `GET /admin/routes` work; the first request that must actually be *proxied*
//! hits a `todo!()` (route match → backend pick → forward) and panics — that panic
//! message is your worklist.

mod balancer;
mod config;
mod error;
mod health;
mod proxy;
mod router;
mod routes;
mod tls;

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};
use tracing::info;

use config::GatewayConfig;
use router::Router;

/// The pooled HTTP client used for every upstream request. `hyper-util`'s
/// `legacy::Client` keeps a per-host connection pool (keep-alive reuse), so the hot
/// path doesn't pay a TCP handshake per request. The body is axum's, so an inbound
/// request can be forwarded without copying it.
pub type UpstreamClient = Client<HttpConnector, Body>;

const DEFAULT_PORT: u16 = 8080;
/// Bound on how long a single upstream TCP connect may take before it's a 502.
const DEFAULT_CONNECT_TIMEOUT_MS: u64 = 2_000;
/// Overall per-request deadline (connect + upstream response), enforced in V1.
const DEFAULT_REQUEST_TIMEOUT_MS: u64 = 10_000;
/// Reject a request body larger than this at the edge (security horizontal).
const DEFAULT_MAX_BODY_BYTES: u64 = 8 * 1024 * 1024;

/// Shared application state, cloned into every handler. The heavy pieces are behind
/// an `Arc` (or are themselves cheap handles), so cloning is cheap.
#[derive(Clone)]
pub struct AppState {
    /// Pooled upstream client (V1).
    pub client: UpstreamClient,
    /// Route table (V2) → upstream pools (V3) → circuit breakers (V4).
    pub router: Arc<Router>,
    /// Renders the Prometheus registry for `GET /metrics`.
    pub prometheus: PrometheusHandle,
    /// Overall per-request deadline, applied in the proxy path (V1).
    pub request_timeout: Duration,
    /// Per-request body size cap, enforced at the edge (security horizontal).
    pub max_body_bytes: u64,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    common_config::load_dotenv();
    common_telemetry::init("info,api_gateway=debug");

    // --- config ---------------------------------------------------------------
    let port: u16 = common_config::parse_or("PORT", DEFAULT_PORT);
    let connect_timeout = Duration::from_millis(common_config::parse_or(
        "UPSTREAM_CONNECT_TIMEOUT_MS",
        DEFAULT_CONNECT_TIMEOUT_MS,
    ));
    let request_timeout = Duration::from_millis(common_config::parse_or(
        "REQUEST_TIMEOUT_MS",
        DEFAULT_REQUEST_TIMEOUT_MS,
    ));
    let max_body_bytes: u64 = common_config::parse_or("MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES);

    // Route table: an explicit JSON file (CONFIG_PATH) or a built-in catch-all over
    // UPSTREAM_BACKENDS so `cargo run` + docker-compose work with zero config files.
    let config = match common_config::or_default("CONFIG_PATH", "") {
        path if !path.is_empty() => GatewayConfig::load(&path)?,
        _ => {
            let backends = common_config::or_default("UPSTREAM_BACKENDS", "127.0.0.1:9001")
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

    // --- pooled upstream client ----------------------------------------------
    // A bounded connect timeout keeps a dead backend from hanging the connect; the
    // pool reuses keep-alive connections so a burst doesn't handshake N times (V1).
    let mut connector = HttpConnector::new();
    connector.set_connect_timeout(Some(connect_timeout));
    connector.set_nodelay(true);
    let client: UpstreamClient = Client::builder(TokioExecutor::new()).build(connector);

    // --- metrics --------------------------------------------------------------
    let prometheus = PrometheusBuilder::new().install_recorder()?;

    // TODO(V4): spawn the active health checker once you build it, e.g.
    //   let probe = Duration::from_millis(common_config::parse_or("HEALTH_PROBE_MS", 2_000));
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
