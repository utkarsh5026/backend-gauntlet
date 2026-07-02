//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the counter/
//! gauge call sites (in [`routes`](crate::routes) and [`ingest`](crate::ingest))
//! stay decoupled from this wiring — they just name a metric. Until [`install`]
//! sets a recorder the macros are no-ops, which is exactly what tests want (no
//! metrics setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from a `/metrics` route
//! (see [`crate::routes::metrics_router`]) for a scrape endpoint. The three
//! metrics the SPEC grades:
//! - [`REDIRECTS_TOTAL`] — redirects served, labelled by cache outcome.
//! - [`CACHE_LOOKUPS_TOTAL`] — every redirect resolution, labelled by outcome;
//!   `hit / sum` is the cache hit ratio.
//! - [`INGEST_QUEUE_DEPTH`] — live depth of the click-ingestion channel.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Redirects served (a 3xx was returned), labelled `cache = hit|miss`.
pub const REDIRECTS_TOTAL: &str = "url_shortener_redirects_total";

/// Cache-aside resolutions on the redirect path, labelled `outcome =
/// hit|miss|negative`. The hit ratio is `hit / (hit + miss + negative)`.
pub const CACHE_LOOKUPS_TOTAL: &str = "url_shortener_cache_lookups_total";

/// Number of buffered clicks waiting in the ingestion channel (a gauge:
/// incremented on accept, decremented as the ingestor drains).
pub const INGEST_QUEUE_DEPTH: &str = "url_shortener_ingest_queue_depth";

/// Install the process-global Prometheus recorder and return a handle used to
/// render the registry for `/metrics`. Call once, from `main`, after telemetry
/// init. Panics if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata for the three SPEC metrics. Call sites only pass the
/// name constant; describing them here keeps the strings single-sourced and
/// gives rustc a direct use of each constant (proc-macro call sites alone do
/// not always satisfy `dead_code`).
fn register_descriptions() {
    let redirects = REDIRECTS_TOTAL;
    let cache_lookups = CACHE_LOOKUPS_TOTAL;
    let ingest_queue = INGEST_QUEUE_DEPTH;
    metrics::describe_counter!(
        redirects,
        "Redirects served (3xx returned), labelled by cache outcome"
    );
    metrics::describe_counter!(
        cache_lookups,
        "Cache-aside resolutions on the redirect path, labelled by outcome"
    );
    metrics::describe_gauge!(
        ingest_queue,
        "Buffered clicks waiting in the ingestion channel"
    );
}
