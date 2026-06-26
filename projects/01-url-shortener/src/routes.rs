//! HTTP routes. The router and handler signatures are wired up; the handler
//! bodies are where you implement the SPEC.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect};
use axum::routing::{get, post};
use axum::{middleware, Json, Router};
use serde::{Deserialize, Serialize};
use tower_http::trace::TraceLayer;

use crate::auth::require_api_key;
use crate::error::AppError;
use crate::AppState;

pub fn router(state: AppState) -> Router {
    // Protected routes require an API key; the public redirect does not.
    let protected = Router::new()
        .route("/api/links", post(create_link))
        .route("/api/links/{slug}/stats", get(link_stats))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            require_api_key,
        ));

    Router::new()
        .route("/healthz", get(healthz))
        .route("/{slug}", get(redirect))
        .merge(protected)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> impl IntoResponse {
    (StatusCode::OK, "ok")
}

// ---- POST /api/links ----

#[derive(Debug, Deserialize)]
pub struct CreateLink {
    pub url: String,
    /// Optional vanity slug (V1 stretch).
    pub custom_slug: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CreatedLink {
    pub slug: String,
    pub short_url: String,
    pub long_url: String,
}

/// TODO:
/// - validate + normalize `body.url` (scheme allowlist, length cap, block SSRF /
///   internal IPs / `javascript:`) — see security checklist.
/// - generate an id+slug via `state.ids` (V1), or use `custom_slug` with a
///   uniqueness check.
/// - INSERT into `links` using a compile-time-checked `sqlx::query!`.
/// - optionally warm the cache.
async fn create_link(
    State(state): State<AppState>,
    Json(body): Json<CreateLink>,
) -> Result<(StatusCode, Json<CreatedLink>), AppError> {
    let _ = (&state, &body);
    todo!("implement link creation (validation + V1 id + insert)")
}

// ---- GET /{slug} (the hot path) ----

/// TODO (this is the performance-critical path):
/// - look up the slug via `state.cache` (cache-aside + stampede protection, V2),
///   falling back to Postgres and populating the cache (positive AND negative).
/// - on hit, fire a `ClickEvent` into `state.clicks` WITHOUT awaiting a DB write
///   (V3) — use `try_send` and decide what to do if the channel is full.
/// - return `301` vs `302` deliberately (think about analytics + caching).
async fn redirect(
    State(state): State<AppState>,
    Path(slug): Path<String>,
) -> Result<Redirect, AppError> {
    let _ = (&state, &slug);
    todo!("implement cached redirect + async click ingestion")
}

// ---- GET /api/links/{slug}/stats ----

#[derive(Debug, Serialize)]
pub struct LinkStats {
    pub slug: String,
    pub long_url: String,
    pub total_clicks: i64,
}

/// TODO: aggregate click stats for the slug. Start with a `COUNT(*)`; later think
/// about how this query behaves once `click_events` has millions of rows (hint:
/// this is exactly why Tier 3 introduces a columnar store).
async fn link_stats(
    State(state): State<AppState>,
    Path(slug): Path<String>,
) -> Result<Json<LinkStats>, AppError> {
    let _ = (&state, &slug);
    todo!("implement stats aggregation")
}
