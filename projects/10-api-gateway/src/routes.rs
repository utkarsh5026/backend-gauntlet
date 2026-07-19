//! HTTP surface: the admin/observability endpoints and the catch-all proxy.
//!
//! Everything except the four `/admin`/`/healthz`/`/metrics` routes is a *proxy
//! target*: the `.fallback` handler resolves a route (V2), picks a backend (V3),
//! and forwards (V1). Those three are `todo!()`, so `GET /healthz` etc. work but the
//! first proxied request panics with the `Vx` it needs — that panic is the worklist.

use axum::extract::{Request, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router as AxumRouter};
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::{proxy, AppState};

/// Build the application router.
pub fn router(state: AppState) -> AxumRouter {
    AxumRouter::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .route("/admin/routes", get(list_routes))
        // Everything else is proxied to an upstream.
        .fallback(proxy_handler)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

/// Gateway liveness — does not touch any upstream.
async fn healthz() -> &'static str {
    "ok"
}

/// Prometheus scrape.
async fn metrics(State(state): State<AppState>) -> String {
    state.prometheus.render()
}

/// The loaded route table, for eyeballing config.
async fn list_routes(State(state): State<AppState>) -> Json<serde_json::Value> {
    let names: Vec<&str> = state.router.route_names().collect();
    Json(serde_json::json!({ "routes": names }))
}

/// Catch-all: proxy the request to whichever backend the route + balancer select.
///
/// TODO(security): before proxying, enforce the edge limits and auth the SPEC lists
/// — body size cap (`state.max_body_bytes`), edge auth, and stripping client-supplied
/// `X-Forwarded-*` / internal headers so a caller can't impersonate the proxy.
async fn proxy_handler(State(state): State<AppState>, req: Request) -> Result<Response, AppError> {
    let method = req.method().clone();
    let path = req.uri().path().to_owned();
    let host = req
        .headers()
        .get(axum::http::header::HOST)
        .and_then(|h| h.to_str().ok())
        .map(str::to_owned);

    // V2: which route? → V3: which backend? → V1: forward it.
    let route = state
        .router
        .match_request(host.as_deref(), &path, &method)
        .ok_or(AppError::NoRoute)?;
    let backend = route
        .upstream
        .balancer
        .pick()
        .ok_or(AppError::NoHealthyBackend)?;

    proxy::forward(&state.client, &backend, req).await
}
