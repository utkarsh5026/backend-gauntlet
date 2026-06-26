//! Shared observability setup, reused by every project in the gauntlet.
//!
//! For now this wires up structured logging via `tracing`. Distributed tracing
//! (OpenTelemetry) and a Prometheus `/metrics` endpoint are added per-project in
//! later tiers — kept out of the base crate so the workspace always compiles
//! cleanly without pinning fast-moving exporter versions.

use tracing_subscriber::{fmt, prelude::*, EnvFilter};

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
