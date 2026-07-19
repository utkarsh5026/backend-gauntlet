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
//! Store/index metrics cover successful object operations, physical blob
//! occupancy, deduplication, garbage collection, ranges, transfer rates, and
//! continuous scrubbing (re-hash auditor).

use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Instant;

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};
use tokio::io::{AsyncRead, ReadBuf};

/// Successful object writes committed to the key index.
pub const OBJECTS_PUT_TOTAL: &str = "object_store_objects_put_total";

/// Successful object GET requests.
pub const OBJECTS_GET_TOTAL: &str = "object_store_objects_get_total";

/// Successful object DELETE requests.
pub const OBJECTS_DELETED_TOTAL: &str = "object_store_objects_deleted_total";

/// Commits skipped because the content-addressed blob already existed.
pub const DEDUP_HITS_TOTAL: &str = "object_store_dedup_hits_total";

/// Unreferenced blobs removed by garbage collection.
pub const GC_BLOBS_RECLAIMED_TOTAL: &str = "object_store_gc_blobs_reclaimed_total";

/// Successful HTTP byte-range responses.
pub const RANGE_REQUESTS_SERVED_TOTAL: &str = "object_store_range_requests_served_total";

/// Bytes occupied by distinct committed blobs.
pub const TOTAL_BYTES_STORED: &str = "object_store_total_bytes_stored";

/// Number of distinct committed blobs.
pub const BLOB_COUNT: &str = "object_store_blob_count";

/// Active PUT or UploadPart request bodies.
pub const IN_FLIGHT_UPLOADS: &str = "object_store_in_flight_uploads";

/// Distribution of successfully stored object sizes.
pub const OBJECT_SIZE_BYTES: &str = "object_store_object_size_bytes";

/// Successful single-PUT throughput in bytes per second.
pub const UPLOAD_THROUGHPUT: &str = "object_store_upload_throughput_bytes_per_second";

/// GET response-body throughput in bytes per second.
pub const DOWNLOAD_THROUGHPUT: &str = "object_store_download_throughput_bytes_per_second";

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

/// Completed background scrub passes over the committed blob tree.
pub const SCRUB_PASSES_TOTAL: &str = "object_store_scrub_passes_total";

/// Blobs whose on-disk bytes still match their content address.
pub const SCRUB_BLOBS_VERIFIED_TOTAL: &str = "object_store_scrub_blobs_verified_total";

/// Blobs whose bytes no longer match their content address (quarantined).
pub const SCRUB_CORRUPTIONS_TOTAL: &str = "object_store_scrub_corruptions_total";

/// Bytes read by the scrubber while re-hashing committed blobs.
pub const SCRUB_BYTES_SCANNED_TOTAL: &str = "object_store_scrub_bytes_scanned_total";

/// Times the scrubber parked on Notify because the blob tree was empty.
pub const SCRUB_IDLE_WAITS_TOTAL: &str = "object_store_scrub_idle_waits_total";

/// Wall time of one full scrub pass, in seconds.
pub const SCRUB_PASS_DURATION: &str = "object_store_scrub_pass_duration_seconds";

/// Install the process-global Prometheus recorder and return a handle used to
/// render the registry for `/metrics`. Call once, from `main`, after telemetry
/// init. Panics if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    let handle = PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder");
    register_descriptions();
    handle
}

/// Register HELP metadata so a `/metrics` scrape is self-describing. Naming the
/// metric here (not only at the emit site) single-sources the string and gives
/// the exporter the unit/description a dashboard reads.
fn register_descriptions() {
    metrics::describe_counter!(
        OBJECTS_PUT_TOTAL,
        "Successful object writes committed to the key index"
    );
    metrics::describe_counter!(OBJECTS_GET_TOTAL, "Successful object GET requests");
    metrics::describe_counter!(OBJECTS_DELETED_TOTAL, "Successful object DELETE requests");
    metrics::describe_counter!(
        DEDUP_HITS_TOTAL,
        "Blob commits skipped because content already existed"
    );
    metrics::describe_counter!(
        GC_BLOBS_RECLAIMED_TOTAL,
        "Unreferenced blobs removed by garbage collection"
    );
    metrics::describe_counter!(
        RANGE_REQUESTS_SERVED_TOTAL,
        "Successful HTTP byte-range responses"
    );
    metrics::describe_gauge!(
        TOTAL_BYTES_STORED,
        "Bytes occupied by distinct committed blobs"
    );
    metrics::describe_gauge!(BLOB_COUNT, "Distinct committed blobs");
    metrics::describe_gauge!(IN_FLIGHT_UPLOADS, "Active PUT or UploadPart request bodies");
    metrics::describe_histogram!(
        OBJECT_SIZE_BYTES,
        "Successfully stored object size in bytes"
    );
    metrics::describe_histogram!(
        UPLOAD_THROUGHPUT,
        "Successful single-PUT throughput in bytes per second"
    );
    metrics::describe_histogram!(
        DOWNLOAD_THROUGHPUT,
        "GET response-body throughput in bytes per second"
    );
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
    metrics::describe_counter!(
        SCRUB_PASSES_TOTAL,
        "Completed background scrub passes over committed blobs"
    );
    metrics::describe_counter!(
        SCRUB_BLOBS_VERIFIED_TOTAL,
        "Blobs whose bytes still match their content address"
    );
    metrics::describe_counter!(
        SCRUB_CORRUPTIONS_TOTAL,
        "Blobs quarantined because bytes no longer match their content address"
    );
    metrics::describe_counter!(
        SCRUB_BYTES_SCANNED_TOTAL,
        "Bytes read while re-hashing committed blobs"
    );
    metrics::describe_counter!(
        SCRUB_IDLE_WAITS_TOTAL,
        "Times the scrubber waited on Notify because no blobs were present"
    );
    metrics::describe_histogram!(
        SCRUB_PASS_DURATION,
        "Wall time of one full scrub pass in seconds"
    );
}

#[derive(Default)]
pub struct InFlightGuard;

impl InFlightGuard {
    pub fn new() -> Self {
        metrics::gauge!(IN_FLIGHT_UPLOADS).increment(1.0);
        Self
    }
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        metrics::gauge!(IN_FLIGHT_UPLOADS).decrement(1.0);
    }
}

/// Measure bytes read over the complete lifetime of a streamed response body.
pub struct ObservedDownload<R> {
    inner: R,
    started: Instant,
    bytes: u64,
    recorded: bool,
}

impl<R> ObservedDownload<R> {
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            started: Instant::now(),
            bytes: 0,
            recorded: false,
        }
    }

    fn record_once(&mut self) {
        if self.recorded {
            return;
        }
        self.recorded = true;
        let elapsed = self.started.elapsed().as_secs_f64();
        if elapsed > 0.0 {
            metrics::histogram!(DOWNLOAD_THROUGHPUT).record(self.bytes as f64 / elapsed);
        }
    }
}

impl<R: AsyncRead + Unpin> AsyncRead for ObservedDownload<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        let before = buf.filled().len();
        let result = Pin::new(&mut this.inner).poll_read(cx, buf);
        if let Poll::Ready(Ok(())) = &result {
            let read = buf.filled().len() - before;
            this.bytes += read as u64;
            if read == 0 {
                this.record_once();
            }
        }
        result
    }
}

impl<R> Drop for ObservedDownload<R> {
    fn drop(&mut self) {
        self.record_once();
    }
}
