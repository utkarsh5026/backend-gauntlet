//! Prometheus metrics for the observability checklist.
//!
//! The [`metrics`] facade writes to a process-global recorder, so the call
//! sites in the store's modules (currently [`multipart`](crate::multipart))
//! just *name* a metric — they stay decoupled from this wiring. Until
//! [`install`] sets a recorder the macros are no-ops, which is exactly what the
//! unit tests want (no metrics setup, no panics).
//!
//! [`install`] returns a [`PrometheusHandle`]; [`crate::routes::metrics_router`]
//! renders it at `GET /metrics` for a scrape endpoint.
//!
//! # Cardinality: why nothing is labelled by bucket/key
//! `bucket` and especially `key` are unbounded, caller-controlled strings. Using
//! them as label values would mint a fresh time series per key and blow the
//! registry up (a cardinality explosion). Per-request identity belongs on the
//! *tracing span* (bounded, sampled, and where `bucket`/`key`/`size` already
//! live — see the `#[tracing::instrument]` attributes in `multipart`), never on
//! a metric label. That split — spans for identity, metrics for aggregates — is
//! the observability lesson.
//!
//! The multipart family (V4), all this module currently owns:
//! - [`MULTIPART_INITIATED_TOTAL`] / [`MULTIPART_COMPLETED_TOTAL`] /
//!   [`MULTIPART_ABORTED_TOTAL`] — the session-lifecycle counters.
//! - [`MULTIPART_PARTS_UPLOADED_TOTAL`] — parts successfully staged.
//! - [`MULTIPART_OPEN_SESSIONS`] — a gauge of live sessions (initiated, not yet
//!   completed or aborted). A steadily climbing value is the SPEC's leak/abuse
//!   signal — never-completed uploads pinning staging space.
//! - [`MULTIPART_OBJECT_BYTES`] — assembled object-size distribution.
//! - [`MULTIPART_PART_BYTES`] / [`MULTIPART_PART_THROUGHPUT`] — per-part size and
//!   streaming throughput (bytes/sec).
//!
//! The store/index PUT/GET/DELETE, dedup-hit, and GC counters the SPEC also
//! grades are not wired yet — add their constants here and emit from the
//! matching modules to finish closing the observability box.

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Multipart uploads initiated (`CreateMultipartUpload`).
pub const MULTIPART_INITIATED_TOTAL: &str = "object_store_multipart_initiated_total";

/// Multipart uploads completed — assembled, committed (V1), and indexed (V3).
pub const MULTIPART_COMPLETED_TOTAL: &str = "object_store_multipart_completed_total";

/// Multipart uploads aborted (`AbortMultipartUpload`), staging reclaimed.
pub const MULTIPART_ABORTED_TOTAL: &str = "object_store_multipart_aborted_total";

/// Parts successfully staged across all sessions (`UploadPart`).
pub const MULTIPART_PARTS_UPLOADED_TOTAL: &str = "object_store_multipart_parts_uploaded_total";

/// Live multipart sessions: incremented on initiate, decremented when a session
/// is completed or aborted. A monotonically climbing gauge means sessions are
/// being opened and never finished — a leak/abuse signal.
pub const MULTIPART_OPEN_SESSIONS: &str = "object_store_multipart_open_sessions";

/// Distribution of assembled multipart object sizes, in bytes.
pub const MULTIPART_OBJECT_BYTES: &str = "object_store_multipart_object_bytes";

/// Distribution of individual uploaded part sizes, in bytes.
pub const MULTIPART_PART_BYTES: &str = "object_store_multipart_part_bytes";

/// Per-part upload throughput, in bytes per second (part size / stream time).
pub const MULTIPART_PART_THROUGHPUT: &str =
    "object_store_multipart_part_throughput_bytes_per_second";

/// Install the process-global Prometheus recorder and return a handle used to
/// render the registry for `/metrics`. Call once, from `main`, after telemetry
/// init. Panics if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata so a `/metrics` scrape is self-describing. Naming the
/// metric here (not only at the emit site) single-sources the string and gives
/// the exporter the unit/description a dashboard reads.
fn register_descriptions() {
    metrics::describe_counter!(MULTIPART_INITIATED_TOTAL, "Multipart uploads initiated");
    metrics::describe_counter!(
        MULTIPART_COMPLETED_TOTAL,
        "Multipart uploads completed (assembled, committed, indexed)"
    );
    metrics::describe_counter!(MULTIPART_ABORTED_TOTAL, "Multipart uploads aborted");
    metrics::describe_counter!(
        MULTIPART_PARTS_UPLOADED_TOTAL,
        "Parts successfully staged across all sessions"
    );
    metrics::describe_gauge!(
        MULTIPART_OPEN_SESSIONS,
        "Live multipart sessions (initiated, not yet completed or aborted)"
    );
    metrics::describe_histogram!(
        MULTIPART_OBJECT_BYTES,
        "Assembled multipart object size in bytes"
    );
    metrics::describe_histogram!(MULTIPART_PART_BYTES, "Uploaded part size in bytes");
    metrics::describe_histogram!(
        MULTIPART_PART_THROUGHPUT,
        "Per-part upload throughput in bytes per second"
    );
}
