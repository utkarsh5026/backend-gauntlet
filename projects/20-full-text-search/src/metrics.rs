//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the call sites
//! (in [`shard`](crate::shard) and [`index`](crate::index)) stay decoupled from this
//! wiring — they just name a metric. Until [`install`] sets a recorder the macros
//! are no-ops, which is exactly what tests want (no setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see
//! [`crate::routes::metrics_router`]). The series the SPEC grades:
//! - [`DOCS_INDEXED_TOTAL`] — documents accepted for indexing.
//! - [`SEARCHES_TOTAL`] — searches served.
//! - [`SEARCH_DURATION`] — a histogram of end-to-end search latency (source of p99).
//! - [`QUERY_CACHE_LOOKUPS_TOTAL`] — labelled `outcome = hit|miss`; `hit / sum` is the ratio.
//! - [`SEGMENTS`] — live segment count per shard (a gauge; the merge backlog signal).
//! - [`MERGES_TOTAL`] — segment merges completed (V4).
//!
//! Wiring the *call sites* is the observability horizontal item — this module just
//! makes `/metrics` render and single-sources the metric names.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Documents accepted for indexing (across all shards).
pub const DOCS_INDEXED_TOTAL: &str = "search_documents_indexed_total";

/// Searches served (a cache hit still counts as a search).
pub const SEARCHES_TOTAL: &str = "search_searches_total";

/// End-to-end search latency, in seconds — rendered as quantiles, so p99 falls out.
pub const SEARCH_DURATION: &str = "search_duration_seconds";

/// Query-cache lookups, labelled `outcome = hit|miss`. Hit ratio = `hit / (hit+miss)`.
pub const QUERY_CACHE_LOOKUPS_TOTAL: &str = "search_query_cache_lookups_total";

/// Live segments in a shard, labelled by `shard`. Climbs on refresh, drops on merge —
/// a rising, un-merged count is the "too many segments" health signal.
pub const SEGMENTS: &str = "search_segments";

/// Segment merges completed (V4).
pub const MERGES_TOTAL: &str = "search_merges_total";

/// Install the process-global Prometheus recorder and return a handle used to render
/// the registry for `/metrics`. Call once, from `main`, after telemetry init. Panics
/// if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata for the graded metrics. Naming each constant here also
/// gives rustc a direct use of it (proc-macro call sites alone don't always satisfy
/// `dead_code`), so the scaffold stays warning-quiet before the call sites exist.
fn register_descriptions() {
    let docs = DOCS_INDEXED_TOTAL;
    let searches = SEARCHES_TOTAL;
    let duration = SEARCH_DURATION;
    let cache = QUERY_CACHE_LOOKUPS_TOTAL;
    let segments = SEGMENTS;
    let merges = MERGES_TOTAL;
    metrics::describe_counter!(docs, "Documents accepted for indexing");
    metrics::describe_counter!(searches, "Searches served");
    metrics::describe_histogram!(duration, "End-to-end search latency, seconds");
    metrics::describe_counter!(cache, "Query-cache lookups, labelled hit|miss");
    metrics::describe_gauge!(segments, "Live segments per shard");
    metrics::describe_counter!(merges, "Segment merges completed");
}
