//! HTTP routes. The router and handler signatures are wired up; the handler
//! bodies are where you implement the SPEC.

use std::sync::Arc;
use std::time::Instant;

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, Method, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{middleware, Json, Router};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::{Deserialize, Serialize};
use tower_governor::errors::GovernorError;
use tower_governor::governor::GovernorConfigBuilder;
use tower_governor::GovernorLayer;
use tower_http::trace::TraceLayer;

use crate::auth::require_api_key;
use crate::cache::Cached;
use crate::error::AppError;
use crate::id_gen::{IdGenerator, CUSTOM_EPOCH_MS};
use crate::ingest::ClickEvent;
use crate::ratelimit::{ApiKeyExtractor, RATE_LIMIT_BURST, RATE_LIMIT_PERIOD_MS};
use crate::url_validate::validate_long_url;
use crate::AppState;

/// The built Vite/React dashboard (`dashboard/dist`), embedded into the binary
/// so `cargo run` serves it at `/` with no external asset files — the successor
/// to the old single-file `include_str!` dashboard. Build it first with
/// `npm --prefix dashboard run build`. In debug builds `rust-embed` reads the
/// folder from disk at runtime (rebuild the frontend, just refresh — no `cargo`
/// rebuild); in release it bakes the bytes into the binary at compile time.
#[derive(rust_embed::RustEmbed)]
#[folder = "dashboard/dist"]
struct Assets;

const MIN_CUSTOM_SLUG_LEN: usize = 1;
const MAX_CUSTOM_SLUG_LEN: usize = 64;

/// Slugs that would collide with first-class HTTP routes.
const RESERVED_SLUGS: &[&str] = &["healthz", "api"];

/// Validate and normalize a user-supplied vanity slug for `POST /api/links`.
///
/// Only called when `custom_slug` is set. Auto-generated slugs from
/// [`IdGenerator::next_id_and_slug`](crate::id_gen::IdGenerator::next_id_and_slug)
/// skip this path.
///
/// Uniqueness is not checked here; duplicate slugs fail at insert time with
/// `"slug already in use"` (Postgres unique constraint on `links.slug`).
///
/// # Errors
///
/// Returns [`AppError::BadRequest`] when the slug is empty, too long, contains
/// disallowed characters, or matches a reserved name.
fn validate_custom_slug(input: &str) -> Result<String, AppError> {
    let slug = input.trim();
    if slug.len() < MIN_CUSTOM_SLUG_LEN || slug.len() > MAX_CUSTOM_SLUG_LEN {
        return Err(AppError::BadRequest(
            "slug length must be 1-64 characters".into(),
        ));
    }
    if !slug
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(AppError::BadRequest(
            "slug may only contain letters, numbers, hyphens, and underscores".into(),
        ));
    }
    let lower = slug.to_ascii_lowercase();
    if RESERVED_SLUGS.contains(&lower.as_str()) {
        return Err(AppError::BadRequest("slug is reserved".into()));
    }
    Ok(slug.to_string())
}

pub fn router(state: AppState) -> Router {
    // Per-API-key rate limit on the write path (tower_governor / GCRA). The
    // GovernorConfig owns the keyed bucket store; cloning the layer shares it.
    // `.methods([POST])` scopes throttling to `POST /api/links` — GET stats and
    // the public redirect are exempt. `expect` only fires on a zero period/burst.
    let rate_limit = Arc::new(
        GovernorConfigBuilder::default()
            .per_millisecond(RATE_LIMIT_PERIOD_MS)
            .burst_size(RATE_LIMIT_BURST)
            .methods(vec![Method::POST])
            .key_extractor(ApiKeyExtractor)
            // Route governor's 429 through our JSON `{"error": ...}` envelope so
            // the limited response matches the rest of the API, while keeping
            // its `retry-after` / `x-ratelimit-*` headers so clients can back off.
            .error_handler(|error| match error {
                GovernorError::TooManyRequests { headers, .. } => {
                    let mut response = AppError::RateLimited.into_response();
                    if let Some(headers) = headers {
                        response.headers_mut().extend(headers);
                    }
                    response
                }
                // `require_api_key` runs first, so a missing key here would mean
                // the layers are mis-ordered; fall back to governor's default.
                mut other => other.as_response(),
            })
            .finish()
            .expect("rate-limit config has non-zero period and burst"),
    );

    // Protected routes require an API key; the public redirect does not.
    let protected = Router::new()
        .route("/api/links", post(create_link))
        .route("/api/links/{slug}/stats", get(link_stats))
        // `route_layer` (not `layer`): these middlewares only wrap routes that
        // match *this* sub-router, never its fallback. With plain `layer`, the
        // auth-wrapped fallback leaks through `.merge()` and ends up guarding the
        // public `/{slug}` redirect (every redirect → 401). `route_layer` is also
        // the right tool for early-returning auth/limit middleware.
        // Layers wrap bottom-up: `require_api_key` is outermost so it runs first
        // and the limiter always sees an authenticated key to bucket on.
        .route_layer(GovernorLayer { config: rate_limit })
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            require_api_key,
        ));

    Router::new()
        .route("/", get(dashboard))
        // Vite emits the hashed JS/CSS bundles under `assets/`, so this wildcard
        // serves them without colliding with the single-segment `/{slug}` redirect.
        .route("/assets/{*path}", get(static_asset))
        .route("/healthz", get(healthz))
        // Public, read-only observability for the demo dashboard: mirrors the
        // redirect's cache-aside resolution but returns JSON (cache outcome +
        // decoded Snowflake) instead of a 3xx. Multi-segment path under `/api`
        // so it never collides with the single-segment `/{slug}` redirect.
        .route("/api/debug/resolve/{slug}", get(resolve_debug))
        .route("/{slug}", get(redirect))
        .merge(protected)
        // One tracing span per request, tagged with a fresh `request_id` (the
        // observability checklist). `make_request_span` lives in
        // `common-telemetry` so every service shares the convention; the
        // per-handler `#[instrument]` spans (e.g. `redirect`) nest under it and
        // inherit the id. The closure pins the body type so type inference
        // resolves the generic helper.
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(|req: &Request<Body>| common_telemetry::make_request_span(req)),
        )
        .with_state(state)
}

/// The `/metrics` scrape endpoint, kept separate from [`router`] because it
/// closes over the Prometheus [`PrometheusHandle`] instead of `AppState` — the
/// recorder is installed once in `main`, never in tests. Public and unauthed,
/// like `/healthz`: a scraper reaches it without an API key.
pub fn metrics_router(handle: PrometheusHandle) -> Router {
    Router::new().route(
        "/metrics",
        get(move || {
            let handle = handle.clone();
            async move { handle.render() }
        }),
    )
}

/// Serves the dashboard's `index.html` (the SPA entrypoint) at `/`.
async fn dashboard() -> Response {
    serve_embedded("index.html")
}

/// Serves the dashboard's hashed bundles from `/assets/{path}`.
async fn static_asset(Path(path): Path<String>) -> Response {
    serve_embedded(&format!("assets/{path}"))
}

/// Look an embedded file up by its path within `dashboard/dist`, returning it
/// with the right `Content-Type` (via the `mime-guess` feature) or a 404 if the
/// file isn't present (e.g. the frontend hasn't been built yet).
fn serve_embedded(path: &str) -> Response {
    match Assets::get(path) {
        Some(content) => (
            [(
                header::CONTENT_TYPE,
                content.metadata.mimetype().to_string(),
            )],
            content.data.into_owned(),
        )
            .into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn healthz() -> impl IntoResponse {
    (StatusCode::OK, "ok")
}

#[derive(Debug, Deserialize)]
pub struct CreateLink {
    pub url: String,
    pub custom_slug: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CreatedLink {
    pub slug: String,
    pub short_url: String,
    pub long_url: String,
}

/// TODO:
/// - optionally warm the cache after insert.
async fn create_link(
    State(state): State<AppState>,
    Json(body): Json<CreateLink>,
) -> Result<(StatusCode, Json<CreatedLink>), AppError> {
    let long_url = validate_long_url(&body.url)?;

    let (id, slug) = match body.custom_slug {
        Some(custom) => (state.ids.next_id(), validate_custom_slug(&custom)?),
        None => state.ids.next_id_and_slug(),
    };

    sqlx::query!(
        "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
        id,
        slug,
        long_url,
    )
    .execute(&state.db)
    .await
    .map_err(|e| match e {
        // 23505 = unique_violation: a custom slug that's already taken.
        sqlx::Error::Database(db) if db.code().as_deref() == Some("23505") => {
            AppError::BadRequest("slug already in use".to_string())
        }
        other => AppError::Database(other),
    })?;

    let short_url = format!("{}/{}", state.base_url.trim_end_matches('/'), slug);
    let link = CreatedLink {
        slug,
        short_url,
        long_url,
    };
    Ok((StatusCode::CREATED, Json(link)))
}

/// Where a slug resolution was ultimately served from — the cache-aside outcome.
/// Exposed to clients as the `X-Cache` header and in the demo's debug JSON.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CacheOutcome {
    /// Served straight from Redis (positive entry).
    Hit,
    /// Redis held a negative entry — short-circuited to 404 without touching Postgres.
    Negative,
    /// Redis missed; Postgres was consulted (and the result back-filled into Redis).
    Miss,
}

impl CacheOutcome {
    /// Lowercase label used for the tracing span field (`hit` / `negative` / `miss`).
    fn label(self) -> &'static str {
        match self {
            CacheOutcome::Hit => "hit",
            CacheOutcome::Negative => "negative",
            CacheOutcome::Miss => "miss",
        }
    }

    /// Uppercase form for the `X-Cache` response header.
    fn header(self) -> &'static str {
        match self {
            CacheOutcome::Hit => "HIT",
            CacheOutcome::Negative => "NEGATIVE",
            CacheOutcome::Miss => "MISS",
        }
    }
}

/// Outcome of resolving a slug through the cache-aside path.
struct Resolved {
    outcome: CacheOutcome,
    /// `Some` when the slug maps to a live link; `None` for a 404 (negative or absent).
    link: Option<(i64, String)>,
}

/// Cache-aside resolution shared by the redirect and the debug endpoint: Redis
/// first, Postgres on miss, populating positive/negative entries. This is the V2
/// hot path — kept in one place so the demo's observability sees exactly what a
/// real redirect sees.
async fn resolve_slug(state: &AppState, slug: &str) -> Result<Resolved, AppError> {
    Ok(match state.cache.get(slug).await? {
        Some(Cached::Found { link_id, long_url }) => Resolved {
            outcome: CacheOutcome::Hit,
            link: Some((link_id, long_url)),
        },
        Some(Cached::Missing) => Resolved {
            outcome: CacheOutcome::Negative,
            link: None,
        },
        None => {
            let row = sqlx::query!("SELECT id, long_url FROM links WHERE slug = $1", slug)
                .fetch_optional(&state.db)
                .await?;
            match row {
                Some(row) => {
                    state.cache.put_found(slug, row.id, &row.long_url).await?;
                    Resolved {
                        outcome: CacheOutcome::Miss,
                        link: Some((row.id, row.long_url)),
                    }
                }
                None => {
                    state.cache.put_missing(slug).await?;
                    Resolved {
                        outcome: CacheOutcome::Miss,
                        link: None,
                    }
                }
            }
        }
    })
}

/// Cache-aside redirect: Redis first, Postgres on miss, populate positive/negative entries.
///
/// Structured per-redirect observability: the span carries `slug`, the cache
/// outcome (`hit` / `negative` / `miss`), and end-to-end `latency_ms`. `cache`
/// and `latency_ms` start `Empty` and are filled at runtime via `Span::record`.
/// The response also carries an `X-Cache` header so the outcome is visible to
/// clients (curl / browser devtools) without reading logs.
#[tracing::instrument(
    name = "redirect",
    skip(state, headers),
    fields(slug = %slug, cache = tracing::field::Empty, latency_ms = tracing::field::Empty)
)]
async fn redirect(
    State(state): State<AppState>,
    Path(slug): Path<String>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let started = Instant::now();
    let span = tracing::Span::current();

    let resolved = resolve_slug(&state, &slug).await?;
    span.record("cache", resolved.outcome.label());
    span.record("latency_ms", started.elapsed().as_millis() as u64);
    tracing::info!("redirect served");

    metrics::counter!(crate::metrics::CACHE_LOOKUPS_TOTAL, "outcome" => resolved.outcome.label())
        .increment(1);

    match resolved.link {
        Some((link_id, long_url)) => {
            record_click(&state, link_id, &headers);
            metrics::counter!(crate::metrics::REDIRECTS_TOTAL, "cache" => resolved.outcome.label())
                .increment(1);
            // 302 Found, not a permanent redirect: 301/308 are cached by browsers
            // and proxies, so repeat visits would skip the server and we'd
            // under-count clicks. A temporary redirect keeps every hit flowing
            // through here for analytics.
            Ok((
                StatusCode::FOUND,
                [
                    (header::LOCATION.as_str(), long_url.as_str()),
                    ("x-cache", resolved.outcome.header()),
                    (header::CACHE_CONTROL.as_str(), "no-store"),
                ],
            )
                .into_response())
        }
        None => Err(AppError::NotFound),
    }
}

#[derive(Debug, Serialize)]
struct SnowflakeParts {
    /// The raw 64-bit id (the slug is this number, base62-encoded).
    id: i64,
    /// Milliseconds since the Unix epoch, recovered from the id's timestamp bits.
    timestamp_unix_ms: u64,
    /// The id generator's custom epoch (the timestamp bits count from here).
    custom_epoch_unix_ms: u64,
    /// Node/worker id baked into the id — why two instances never collide.
    node_id: u16,
    /// Per-millisecond sequence counter.
    sequence: u16,
}

#[derive(Debug, Serialize)]
struct ResolveDebug {
    slug: String,
    /// `true` when the slug maps to a live link.
    found: bool,
    long_url: Option<String>,
    /// Cache outcome: `hit` | `miss` | `negative`.
    cache: &'static str,
    /// Human-friendly store the answer came from.
    served_from: &'static str,
    /// End-to-end resolution latency in milliseconds.
    latency_ms: f64,
    /// Decoded Snowflake fields, present when the slug resolves to a link.
    snowflake: Option<SnowflakeParts>,
}

/// Read-only observability endpoint for the demo dashboard. Runs the same
/// cache-aside [`resolve_slug`] a redirect would, but returns JSON describing
/// the cache outcome and the decoded Snowflake id instead of a 3xx. Public
/// (like the redirect) and does not record a click — it's a pure inspector.
async fn resolve_debug(
    State(state): State<AppState>,
    Path(slug): Path<String>,
) -> Result<Json<ResolveDebug>, AppError> {
    let started = Instant::now();
    let resolved = resolve_slug(&state, &slug).await?;
    let latency_ms = started.elapsed().as_secs_f64() * 1_000.0;

    let served_from = match resolved.outcome {
        CacheOutcome::Hit => "redis (cache hit)",
        CacheOutcome::Negative => "redis (negative cache)",
        CacheOutcome::Miss => "postgres (cache miss → back-filled)",
    };

    let snowflake = resolved.link.as_ref().map(|(id, _)| {
        let parts = IdGenerator::decode(*id);
        SnowflakeParts {
            id: *id,
            timestamp_unix_ms: parts.timestamp_ms + CUSTOM_EPOCH_MS,
            custom_epoch_unix_ms: CUSTOM_EPOCH_MS,
            node_id: parts.node_id,
            sequence: parts.sequence,
        }
    });

    Ok(Json(ResolveDebug {
        slug,
        found: resolved.link.is_some(),
        long_url: resolved.link.map(|(_, url)| url),
        cache: resolved.outcome.label(),
        served_from,
        latency_ms,
        snowflake,
    }))
}

fn record_click(state: &AppState, link_id: i64, headers: &HeaderMap) {
    let referer = header_value(headers, "referer");
    let user_agent = header_value(headers, "user-agent");
    state.clicks.accept(ClickEvent {
        link_id,
        referer,
        user_agent,
        ip_hash: None,
    });
}

fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
}

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

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    #[test]
    fn accepts_valid_custom_slug() {
        assert_eq!(validate_custom_slug("my-brand_2").unwrap(), "my-brand_2");
    }

    #[test]
    fn trims_custom_slug() {
        assert_eq!(validate_custom_slug("  promo  ").unwrap(), "promo");
    }

    #[test]
    fn rejects_empty_custom_slug() {
        assert!(validate_custom_slug("").is_err());
        assert!(validate_custom_slug("   ").is_err());
    }

    #[test]
    fn rejects_invalid_custom_slug_chars() {
        assert!(validate_custom_slug("my brand").is_err());
        assert!(validate_custom_slug("café").is_err());
        assert!(validate_custom_slug("a/b").is_err());
    }

    #[test]
    fn rejects_reserved_custom_slug() {
        assert!(validate_custom_slug("healthz").is_err());
        assert!(validate_custom_slug("API").is_err());
    }

    #[test]
    fn rejects_too_long_custom_slug() {
        let slug = "a".repeat(MAX_CUSTOM_SLUG_LEN + 1);
        assert!(validate_custom_slug(&slug).is_err());
    }

    // Property-based tests for `validate_custom_slug`. The example tests above pin
    // down specific cases; these assert the *invariants* hold across the whole
    // input space proptest can reach — the cheapest way to catch an edge the
    // hand-written cases miss (and proptest shrinks any failure to a minimal repro).
    proptest! {
        /// Any string drawn from the allowed alphabet, within the length bound and
        /// not reserved, is accepted and returned verbatim — there is no surrounding
        /// whitespace to trim, so the slug must pass through unchanged.
        #[test]
        fn prop_wellformed_slugs_pass_through(s in "[A-Za-z0-9_-]{1,64}") {
            prop_assume!(!RESERVED_SLUGS.contains(&s.to_ascii_lowercase().as_str()));
            prop_assert_eq!(validate_custom_slug(&s).unwrap(), s);
        }

        /// The core safety invariant: *whatever* the input, any slug the validator
        /// accepts satisfies every rule it promises. If this ever fails, a bad value
        /// is reaching the database.
        #[test]
        fn prop_accepted_output_always_satisfies_rules(s in ".{0,120}") {
            if let Ok(out) = validate_custom_slug(&s) {
                prop_assert!((MIN_CUSTOM_SLUG_LEN..=MAX_CUSTOM_SLUG_LEN).contains(&out.len()));
                prop_assert!(out
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
                prop_assert!(!RESERVED_SLUGS.contains(&out.to_ascii_lowercase().as_str()));
                prop_assert_eq!(out.trim(), out.as_str(), "accepted slug still had surrounding whitespace");
            }
        }

        /// Validation is a fixed point: feeding an accepted slug back in is a no-op.
        /// (The output is already trimmed and valid, so it must re-validate to itself.)
        #[test]
        fn prop_validation_is_idempotent(s in ".{0,120}") {
            if let Ok(first) = validate_custom_slug(&s) {
                let second = validate_custom_slug(&first).expect("an accepted slug must re-validate");
                prop_assert_eq!(first, second);
            }
        }

        /// Anything past the length cap is rejected even with a perfectly clean
        /// alphabet — proving the bound, not the charset, does the rejecting.
        #[test]
        fn prop_overlong_slugs_are_rejected(s in "[A-Za-z0-9_-]{65,200}") {
            prop_assert!(validate_custom_slug(&s).is_err());
        }

        /// A single disallowed character anywhere in an otherwise in-range slug
        /// rejects the whole thing. `bad` sits between two valid halves, so it is
        /// interior and survives trimming; total length stays well under the cap,
        /// isolating the charset rule as the cause of rejection.
        #[test]
        fn prop_one_disallowed_char_rejects(
            head in "[A-Za-z0-9_-]{1,20}",
            bad in "[^A-Za-z0-9_-]",
            tail in "[A-Za-z0-9_-]{1,20}",
        ) {
            let s = format!("{head}{bad}{tail}");
            prop_assert!(validate_custom_slug(&s).is_err());
        }

        /// Reserved names are rejected regardless of letter case or surrounding
        /// whitespace — `flips` randomizes the case of each character and the pads
        /// add trimmable whitespace.
        #[test]
        fn prop_reserved_slugs_always_rejected(
            word in prop::sample::select(RESERVED_SLUGS.to_vec()),
            flips in prop::collection::vec(any::<bool>(), 0..8),
            lpad in "[ \t]{0,4}",
            rpad in "[ \t]{0,4}",
        ) {
            let cased: String = word
                .chars()
                .enumerate()
                .map(|(i, c)| {
                    if flips.get(i).copied().unwrap_or(false) {
                        c.to_ascii_uppercase()
                    } else {
                        c
                    }
                })
                .collect();
            let padded = format!("{lpad}{cased}{rpad}");
            prop_assert!(validate_custom_slug(&padded).is_err());
        }
    }
}

/// The `/metrics` scrape endpoint (observability checklist). Uses a
/// *locally-scoped* recorder via [`metrics::with_local_recorder`] so the test
/// records and renders against its own registry — no global recorder install,
/// so it never races the rest of the suite and needs no DB/Redis.
#[cfg(test)]
mod metrics_tests {
    use axum::body::{to_bytes, Body};
    use axum::http::{Request, StatusCode};
    use metrics_exporter_prometheus::PrometheusBuilder;
    use tower::ServiceExt;

    use crate::metrics::{CACHE_LOOKUPS_TOTAL, REDIRECTS_TOTAL};
    use crate::routes::metrics_router;

    #[tokio::test]
    async fn metrics_endpoint_renders_recorded_counters() {
        let recorder = PrometheusBuilder::new().build_recorder();
        let handle = recorder.handle();

        // Record a couple of samples against this recorder only.
        metrics::with_local_recorder(&recorder, || {
            metrics::counter!(REDIRECTS_TOTAL, "cache" => "hit").increment(2);
            metrics::counter!(CACHE_LOOKUPS_TOTAL, "outcome" => "hit").increment(2);
            metrics::counter!(CACHE_LOOKUPS_TOTAL, "outcome" => "miss").increment(1);
        });

        let request = Request::builder()
            .method("GET")
            .uri("/metrics")
            .body(Body::empty())
            .unwrap();
        let response = metrics_router(handle).oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);

        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let body = String::from_utf8(bytes.to_vec()).unwrap();

        // Prometheus exposition of the samples above, so a scraper can compute
        // both the redirect count and the cache hit ratio (hit / hit+miss).
        assert!(body.contains(REDIRECTS_TOTAL), "redirects metric exported");
        assert!(
            body.contains(CACHE_LOOKUPS_TOTAL),
            "cache-lookup metric exported"
        );
        assert!(
            body.contains("cache=\"hit\""),
            "redirect cache label present"
        );
        assert!(
            body.contains("outcome=\"miss\""),
            "cache miss label present"
        );
    }
}

/// Full-stack route tests for `POST /api/links` and `GET /{slug}`.
///
/// Each test runs against a fresh, migrated database from `#[sqlx::test]`
/// (auto-dropped — full isolation under parallel `cargo test`). Cache state
/// lives in a scoped Redis namespace via [`RedisTestScope`]; tests that write
/// cache entries `track` their slugs and `cleanup` at the end.
#[cfg(test)]
mod route_tests {
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use serde_json::{json, Value};
    use sqlx::PgPool;
    use tower::ServiceExt;

    use crate::cache::Cached;
    use crate::routes::router;
    use crate::test_support::{app_state_with_db, unique_slug, RedisTestScope};
    use crate::AppState;

    const API_KEY: &str = "test-key";

    /// [`AppState`] backed by the `#[sqlx::test]` pool plus a scoped Redis cache.
    /// Returns the scope so cache-touching tests can `track`/`cleanup` their keys.
    async fn state_and_redis(pool: PgPool) -> (AppState, RedisTestScope) {
        let redis = RedisTestScope::new().await;
        let cache = redis.cache.clone();
        let state = app_state_with_db(cache, &[API_KEY], pool).await;
        (state, redis)
    }

    /// Drive `POST /api/links` through the real router; returns `(status, json)`.
    async fn post_link(state: AppState, api_key: Option<&str>, body: Value) -> (StatusCode, Value) {
        let mut builder = Request::builder()
            .method("POST")
            .uri("/api/links")
            .header(header::CONTENT_TYPE, "application/json");
        if let Some(key) = api_key {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {key}"));
        }
        let request = builder.body(Body::from(body.to_string())).unwrap();

        let response = router(state).oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    /// Drive `GET /{slug}` through the router; returns `(status, Location header)`.
    async fn get_redirect(state: AppState, slug: &str) -> (StatusCode, Option<String>) {
        let request = Request::builder()
            .method("GET")
            .uri(format!("/{slug}"))
            .body(Body::empty())
            .unwrap();

        let response = router(state).oneshot(request).await.unwrap();
        let status = response.status();
        let location = response
            .headers()
            .get(header::LOCATION)
            .map(|v| v.to_str().unwrap().to_owned());
        (status, location)
    }

    // ---- POST /api/links ----

    #[sqlx::test]
    async fn auto_slug_persists_and_returns_created(pool: PgPool) {
        let (state, _redis) = state_and_redis(pool.clone()).await;

        let (status, body) = post_link(
            state,
            Some(API_KEY),
            json!({ "url": "https://example.com/page" }),
        )
        .await;

        assert_eq!(status, StatusCode::CREATED);
        let slug = body["slug"].as_str().expect("slug in response");
        assert!(!slug.is_empty(), "auto-generated slug should be non-empty");
        assert_eq!(body["long_url"], "https://example.com/page");
        assert_eq!(body["short_url"], format!("http://localhost:8080/{slug}"));

        let row = sqlx::query!("SELECT long_url FROM links WHERE slug = $1", slug)
            .fetch_one(&pool)
            .await
            .expect("link row persisted");
        assert_eq!(row.long_url, "https://example.com/page");
    }

    #[sqlx::test]
    async fn honors_custom_slug(pool: PgPool) {
        let (state, _redis) = state_and_redis(pool).await;

        let (status, body) = post_link(
            state,
            Some(API_KEY),
            json!({ "url": "https://example.com", "custom_slug": "promo" }),
        )
        .await;

        assert_eq!(status, StatusCode::CREATED);
        assert_eq!(body["slug"], "promo");
        assert_eq!(body["short_url"], "http://localhost:8080/promo");
    }

    #[sqlx::test]
    async fn duplicate_custom_slug_is_rejected(pool: PgPool) {
        let (state, _redis) = state_and_redis(pool).await;
        let body = json!({ "url": "https://example.com", "custom_slug": "dup" });

        let (first, _) = post_link(state.clone(), Some(API_KEY), body.clone()).await;
        assert_eq!(first, StatusCode::CREATED);

        let (second, json) = post_link(state, Some(API_KEY), body).await;
        assert_eq!(second, StatusCode::BAD_REQUEST);
        assert_eq!(json["error"], "bad request: slug already in use");
    }

    #[sqlx::test]
    async fn non_https_url_is_rejected(pool: PgPool) {
        let (state, _redis) = state_and_redis(pool).await;

        let (status, _) =
            post_link(state, Some(API_KEY), json!({ "url": "http://example.com" })).await;

        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[sqlx::test]
    async fn missing_api_key_is_unauthorized(pool: PgPool) {
        let (state, _redis) = state_and_redis(pool.clone()).await;

        let (status, _) = post_link(state, None, json!({ "url": "https://example.com" })).await;

        assert_eq!(status, StatusCode::UNAUTHORIZED);
        let count = sqlx::query_scalar!("SELECT COUNT(*) FROM links")
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(count, Some(0));
    }

    // ---- GET /{slug} ----

    #[sqlx::test]
    async fn redirect_unknown_slug_is_not_found_and_negative_cached(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool).await;
        let slug = unique_slug("missing");

        let (status, location) = get_redirect(state, &slug).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert!(location.is_none());

        // Cache-aside negative caching: the miss is remembered so the next hit
        // skips Postgres.
        redis.track(&slug);
        assert_eq!(redis.cache.get(&slug).await.unwrap(), Some(Cached::Missing));

        redis.cleanup().await;
    }

    #[sqlx::test]
    async fn redirect_follows_db_link_and_warms_cache(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("go");
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            1_i64,
            slug,
            "https://example.com/dest",
        )
        .execute(&pool)
        .await
        .unwrap();

        // Cache is cold, so this resolves via Postgres.
        let (status, location) = get_redirect(state, &slug).await;
        assert_eq!(status, StatusCode::FOUND); // 302 — temporary, so every click is counted
        assert_eq!(location.as_deref(), Some("https://example.com/dest"));

        // ...and the DB hit warmed the cache for next time.
        redis.track(&slug);
        assert_eq!(
            redis.cache.get(&slug).await.unwrap(),
            Some(Cached::Found {
                link_id: 1,
                long_url: "https://example.com/dest".into(),
            }),
        );

        redis.cleanup().await;
    }

    #[sqlx::test]
    async fn redirect_sets_no_store_cache_control(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("cc");
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            1_i64,
            slug,
            "https://example.com/dest",
        )
        .execute(&pool)
        .await
        .unwrap();

        let request = Request::builder()
            .method("GET")
            .uri(format!("/{slug}"))
            .body(Body::empty())
            .unwrap();
        let response = router(state).oneshot(request).await.unwrap();

        // Redirects must be uncacheable so browsers/proxies can't skip the
        // server on repeat visits — otherwise we'd under-count clicks.
        assert_eq!(response.status(), StatusCode::FOUND);
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store",
        );

        redis.cleanup().await;
    }

    #[sqlx::test]
    async fn redirect_serves_from_cache_without_touching_db(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool).await;
        let slug = unique_slug("cached");

        // Pre-seed the cache only; there is NO DB row, so a 308 proves the
        // response came from Redis, not Postgres.
        redis
            .cache
            .put_found(&slug, 7, "https://cached.example.com/x")
            .await
            .unwrap();
        redis.track(&slug);

        let (status, location) = get_redirect(state, &slug).await;
        assert_eq!(status, StatusCode::FOUND);
        assert_eq!(location.as_deref(), Some("https://cached.example.com/x"));

        redis.cleanup().await;
    }

    #[sqlx::test]
    async fn redirect_negative_cache_short_circuits_db(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("neg");

        // The row exists in Postgres...
        sqlx::query!(
            "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
            5_i64,
            slug,
            "https://real.example.com",
        )
        .execute(&pool)
        .await
        .unwrap();
        // ...but a cached `Missing` must win and short-circuit to a 404.
        redis.cache.put_missing(&slug).await.unwrap();
        redis.track(&slug);

        let (status, _) = get_redirect(state, &slug).await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        redis.cleanup().await;
    }

    // ---- create_link -> redirect round-trip ----

    #[sqlx::test]
    async fn create_link_then_redirect_resolves_to_original_url(pool: PgPool) {
        let (state, mut redis) = state_and_redis(pool.clone()).await;
        let slug = unique_slug("flow");

        // 1. Create through the authenticated write endpoint.
        let (create_status, body) = post_link(
            state.clone(),
            Some(API_KEY),
            json!({ "url": "https://example.com/landing", "custom_slug": slug }),
        )
        .await;
        assert_eq!(create_status, StatusCode::CREATED);
        assert_eq!(body["slug"], slug);

        // 2. The public redirect resolves the freshly-created slug.
        let (redirect_status, location) = get_redirect(state, &slug).await;
        assert_eq!(redirect_status, StatusCode::FOUND);
        assert_eq!(location.as_deref(), Some("https://example.com/landing"));

        // The redirect's DB hit warmed the cache with the row's real Snowflake id.
        let id = sqlx::query_scalar!("SELECT id FROM links WHERE slug = $1", slug)
            .fetch_one(&pool)
            .await
            .unwrap();
        redis.track(&slug);
        assert_eq!(
            redis.cache.get(&slug).await.unwrap(),
            Some(Cached::Found {
                link_id: id,
                long_url: "https://example.com/landing".into(),
            }),
        );

        redis.cleanup().await;
    }
}
