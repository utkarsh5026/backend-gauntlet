//! API gateway / L7 reverse proxy — library surface.
//!
//! `main.rs` is a thin binary over this crate. The modules, [`AppState`] and the
//! pooled [`UpstreamClient`] live here (rather than inside `main.rs`) so the
//! integration tests in `tests/` can drive the *real* code path — the same
//! [`proxy::forward`] a client's bytes travel through over the wire.
//!
//! The learning lives in the modules marked `TODO(Vx)`: the streaming forwarding
//! core (V1, [`proxy`]), the routing engine (V2, [`router`]), the load balancer
//! (V3, [`balancer`]), and health checking + circuit breaking (V4, [`health`]).
//! See SPEC.md.

pub mod balancer;
pub mod config;
pub mod error;
pub mod health;
pub mod proxy;
pub mod router;
pub mod routes;
pub mod tls;

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use metrics_exporter_prometheus::PrometheusHandle;

use router::Router;

/// The pooled HTTP client used for every upstream request. `hyper-util`'s
/// `legacy::Client` keeps a per-host connection pool (keep-alive reuse), so the hot
/// path doesn't pay a TCP handshake per request. The body is axum's, so an inbound
/// request can be forwarded without copying it.
pub type UpstreamClient = Client<HttpConnector, Body>;

pub const DEFAULT_PORT: u16 = 8080;
/// Bound on how long a single upstream TCP connect may take before it's a 502.
pub const DEFAULT_CONNECT_TIMEOUT_MS: u64 = 2_000;
/// Overall per-request deadline (connect + upstream response), enforced in V1.
pub const DEFAULT_REQUEST_TIMEOUT_MS: u64 = 10_000;
/// Reject a request body larger than this at the edge (security horizontal).
pub const DEFAULT_MAX_BODY_BYTES: u64 = 8 * 1024 * 1024;

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

/// Build the pooled upstream client (plumbing).
///
/// A bounded connect timeout keeps a dead backend from hanging the connect; the
/// pool reuses keep-alive connections so a burst doesn't handshake N times (V1).
/// `main` and the integration tests share this so the tests exercise the same
/// pooling behaviour production does.
pub fn upstream_client(connect_timeout: Duration) -> UpstreamClient {
    let mut connector = HttpConnector::new();
    connector.set_connect_timeout(Some(connect_timeout));
    connector.set_nodelay(true);
    Client::builder(TokioExecutor::new()).build(connector)
}
