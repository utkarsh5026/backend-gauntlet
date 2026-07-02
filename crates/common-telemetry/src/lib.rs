//! Shared observability setup, reused by every project in the gauntlet.
//!
//! For now this wires up structured logging via `tracing`. Distributed tracing
//! (OpenTelemetry) and a Prometheus `/metrics` endpoint are added per-project in
//! later tiers — kept out of the base crate so the workspace always compiles
//! cleanly without pinning fast-moving exporter versions.

use http::Request;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};
use uuid::Uuid;

/// Initialize the global tracing subscriber.
///
/// - Log level is controlled by the `RUST_LOG` env var (e.g. `RUST_LOG=info,sqlx=warn`).
///   If unset, falls back to the provided `default_directive`.
/// - Set `LOG_FORMAT=json` for machine-readable logs (what you'd ship to a log
///   aggregator); anything else gives human-friendly pretty output for local dev.
///
/// Call this once, as early as possible in `main`. Calling it twice is a no-op-ish
/// error (the second call fails to set the global default), so don't.
pub fn init(default_directive: &str) {
    let env_filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(default_directive));

    let json = std::env::var("LOG_FORMAT")
        .map(|v| v.eq_ignore_ascii_case("json"))
        .unwrap_or(false);

    let registry = tracing_subscriber::registry().with(env_filter);

    if json {
        registry
            .with(fmt::layer().json().with_current_span(true))
            .init();
    } else {
        registry.with(fmt::layer().with_target(true)).init();
    }

    tracing::info!(json_logs = json, "telemetry initialized");
}

/// Open one tracing span per HTTP request, tagged with a freshly-minted
/// `request_id` (plus method and path). Wire it into
/// [`tower_http::trace::TraceLayer`] via `.make_span_with(...)` so every log
/// line emitted while handling a request inherits the id — the thread that
/// stitches a redirect's `cache`/`latency_ms` fields back to the request that
/// produced them.
///
/// Kept here (not per-project) because a request-scoped span with an id is a
/// cross-cutting concern every service in the gauntlet wants, and it depends
/// only on `http` + `tracing` — no web framework.
///
/// ```ignore
/// use tower_http::trace::TraceLayer;
/// let layer = TraceLayer::new_for_http()
///     .make_span_with(common_telemetry::make_request_span);
/// ```
pub fn make_request_span<B>(request: &Request<B>) -> tracing::Span {
    tracing::info_span!(
        "http_request",
        request_id = %Uuid::new_v4(),
        method = %request.method(),
        path = %request.uri().path(),
    )
}
