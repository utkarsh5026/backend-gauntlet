//! V1 — The reverse-proxy forwarding core.
//!
//! Module: `src/proxy.rs`. This is the byte path: take an inbound request, rewrite
//! it for the chosen backend, stream it upstream over the pooled client, and stream
//! the response back — all without buffering bodies and without leaking hop-by-hop
//! headers between connections. `forward` is the `todo!()`. See SPEC.md V1.

use axum::extract::Request;
use axum::response::Response;

use crate::balancer::Backend;
use crate::error::AppError;
use crate::UpstreamClient;

/// Forward `req` to `backend` and stream the response back to the client.
///
/// TODO(V1): the forwarding core.
///  1. Build the upstream URI from `backend.addr` + the request's path & query;
///     keep method, and copy end-to-end headers only.
///  2. **Strip hop-by-hop headers** (RFC 7230 §6.1: `Connection`, `Keep-Alive`,
///     `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, `Proxy-*`, plus anything
///     named in the inbound `Connection` header) in *both* directions.
///  3. Set provenance: append the client to `X-Forwarded-For`, set
///     `X-Forwarded-Proto`/`Host`, add a `Via` — don't blindly trust an inbound
///     `X-Forwarded-For`.
///  4. Send it via the pooled `client` (keep-alive reuse), enforcing the request
///     deadline; map the upstream `Response<Incoming>` body back to an axum body
///     **by streaming** (no full buffering).
///  5. A connect/transport failure → `AppError::BadGateway`; a timeout →
///     `AppError::GatewayTimeout`. Never panic, never hang.
///
/// The `client`, `backend`, and `req` are everything you need; the deadline and
/// body cap live on `AppState` (thread them in when you wire this up).
pub async fn forward(
    client: &UpstreamClient,
    backend: &Backend,
    req: Request,
) -> Result<Response, AppError> {
    let _ = (client, backend, req);
    todo!("V1: rewrite → strip hop-by-hop → stream upstream → stream response back")
}
