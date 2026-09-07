"""Prometheus metrics for the observability checklist.

`prometheus_client` metrics are process-global objects, so a call site just
imports the one it needs and increments it — the same decoupling Rust got from
the `metrics` facade, without the recorder-installation dance. They register
themselves with the default registry at import, and `common_telemetry`'s
`/metrics` route renders that registry.

# Cardinality: why nothing is labelled by bucket or key

`bucket` and especially `key` are unbounded, caller-controlled strings. Using
them as label values mints a fresh time series per key and blows the registry
up — a cardinality explosion that takes the scrape endpoint down with it.
Per-request identity belongs on the **log line**, where it is bounded by
retention and already carries `bucket` / `key` / `size` via structlog's
contextvars. That split — logs for identity, metrics for aggregates — is the
observability lesson, and it is the reason every metric here is label-free.

Histogram buckets are chosen per metric rather than left at the client default
(which is tuned for request latencies in seconds and is useless for byte
counts): object sizes span bytes to gigabytes, throughput spans KB/s to GB/s,
so both use powers of ten.
"""

from __future__ import annotations

from types import TracebackType

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "BLOB_COUNT",
    "DEDUP_HITS",
    "DOWNLOAD_THROUGHPUT",
    "GC_BLOBS_RECLAIMED",
    "IN_FLIGHT_UPLOADS",
    "InFlightUpload",
    "MULTIPART_ABORTED",
    "MULTIPART_COMPLETED",
    "MULTIPART_INITIATED",
    "MULTIPART_OBJECT_BYTES",
    "MULTIPART_OPEN_SESSIONS",
    "MULTIPART_PARTS_UPLOADED",
    "MULTIPART_PART_BYTES",
    "MULTIPART_PART_THROUGHPUT",
    "OBJECTS_DELETED",
    "OBJECTS_GET",
    "OBJECTS_PUT",
    "OBJECT_SIZE_BYTES",
    "RANGE_REQUESTS_SERVED",
    "SCRUB_BLOBS_VERIFIED",
    "SCRUB_BYTES_SCANNED",
    "SCRUB_CORRUPTIONS",
    "SCRUB_IDLE_WAITS",
    "SCRUB_PASSES",
    "SCRUB_PASS_DURATION",
    "TOTAL_BYTES_STORED",
    "UPLOAD_THROUGHPUT",
    "LIFECYCLE_BYTES_RECLAIMED",
    "LIFECYCLE_OBJECTS_EXPIRED",
    "LIFECYCLE_BLOBS_TIERED",
]

_BYTE_BUCKETS = (
    1024.0,
    16 * 1024.0,
    256 * 1024.0,
    1024 * 1024.0,
    16 * 1024 * 1024.0,
    256 * 1024 * 1024.0,
    1024 * 1024 * 1024.0,
    float("inf"),
)
"""Powers of two from 1 KiB to 1 GiB — object and part sizes."""

_RATE_BUCKETS = (
    1e5,
    1e6,
    1e7,
    5e7,
    1e8,
    5e8,
    1e9,
    float("inf"),
)
"""100 KB/s to 1 GB/s — transfer rates, where an order of magnitude is the unit
of interest and anything finer is noise."""

# ── Object lifecycle counters ───────────────────────────────────────────────

OBJECTS_PUT = Counter(
    "object_store_objects_put_total",
    "Successful object writes committed to the key index",
)
OBJECTS_GET = Counter("object_store_objects_get_total", "Successful object GET requests")
OBJECTS_DELETED = Counter("object_store_objects_deleted_total", "Successful object DELETE requests")
DEDUP_HITS = Counter(
    "object_store_dedup_hits_total",
    "Blob commits skipped because the content already existed",
)
GC_BLOBS_RECLAIMED = Counter(
    "object_store_gc_blobs_reclaimed_total",
    "Unreferenced blobs removed by garbage collection",
)
RANGE_REQUESTS_SERVED = Counter(
    "object_store_range_requests_served_total", "Successful HTTP byte-range responses"
)

# ── Occupancy gauges ────────────────────────────────────────────────────────

TOTAL_BYTES_STORED = Gauge(
    "object_store_total_bytes_stored", "Bytes occupied by distinct committed blobs"
)
BLOB_COUNT = Gauge("object_store_blob_count", "Distinct committed blobs")
IN_FLIGHT_UPLOADS = Gauge(
    "object_store_in_flight_uploads", "Active PUT or UploadPart request bodies"
)

# ── Size and throughput distributions ───────────────────────────────────────

OBJECT_SIZE_BYTES = Histogram(
    "object_store_object_size_bytes",
    "Successfully stored object size in bytes",
    buckets=_BYTE_BUCKETS,
)
UPLOAD_THROUGHPUT = Histogram(
    "object_store_upload_throughput_bytes_per_second",
    "Successful single-PUT throughput in bytes per second",
    buckets=_RATE_BUCKETS,
)
DOWNLOAD_THROUGHPUT = Histogram(
    "object_store_download_throughput_bytes_per_second",
    "GET response-body throughput in bytes per second",
    buckets=_RATE_BUCKETS,
)

# ── Multipart (V4) ──────────────────────────────────────────────────────────

MULTIPART_INITIATED = Counter(
    "object_store_multipart_initiated_total", "Multipart uploads initiated"
)
MULTIPART_COMPLETED = Counter(
    "object_store_multipart_completed_total",
    "Multipart uploads completed (assembled, committed, indexed)",
)
MULTIPART_ABORTED = Counter("object_store_multipart_aborted_total", "Multipart uploads aborted")
MULTIPART_PARTS_UPLOADED = Counter(
    "object_store_multipart_parts_uploaded_total",
    "Parts successfully staged across all sessions",
)
MULTIPART_OPEN_SESSIONS = Gauge(
    "object_store_multipart_open_sessions",
    "Live multipart sessions (initiated, not yet completed or aborted)",
)
"""A monotonically climbing value here is the SPEC's leak signal: sessions
opened and never finished, each pinning staged part bytes on disk."""

MULTIPART_OBJECT_BYTES = Histogram(
    "object_store_multipart_object_bytes",
    "Assembled multipart object size in bytes",
    buckets=_BYTE_BUCKETS,
)
MULTIPART_PART_BYTES = Histogram(
    "object_store_multipart_part_bytes",
    "Uploaded part size in bytes",
    buckets=_BYTE_BUCKETS,
)
MULTIPART_PART_THROUGHPUT = Histogram(
    "object_store_multipart_part_throughput_bytes_per_second",
    "Per-part upload throughput in bytes per second",
    buckets=_RATE_BUCKETS,
)

# ── Continuous scrubbing ────────────────────────────────────────────────────

SCRUB_PASSES = Counter(
    "object_store_scrub_passes_total",
    "Completed background scrub passes over committed blobs",
)
SCRUB_BLOBS_VERIFIED = Counter(
    "object_store_scrub_blobs_verified_total",
    "Blobs whose bytes still match their content address",
)
SCRUB_CORRUPTIONS = Counter(
    "object_store_scrub_corruptions_total",
    "Blobs quarantined because their bytes no longer match their content address",
)
SCRUB_BYTES_SCANNED = Counter(
    "object_store_scrub_bytes_scanned_total",
    "Bytes read while re-hashing committed blobs",
)
SCRUB_IDLE_WAITS = Counter(
    "object_store_scrub_idle_waits_total",
    "Times the scrubber parked because no blobs were present",
)
SCRUB_PASS_DURATION = Histogram(
    "object_store_scrub_pass_duration_seconds", "Wall time of one full scrub pass"
)

# ── Lifecycle sweeps ────────────────────────────────────────────────────────

LIFECYCLE_OBJECTS_EXPIRED = Counter(
    "object_store_lifecycle_objects_expired_total",
    "Live objects expired by a lifecycle rule",
)
LIFECYCLE_BLOBS_TIERED = Counter(
    "object_store_lifecycle_blobs_tiered_total",
    "Blobs migrated to the compressed cold tier",
)
LIFECYCLE_BYTES_RECLAIMED = Counter(
    "object_store_lifecycle_bytes_reclaimed_total",
    "Bytes saved on disk by cold-tier compression",
)


class InFlightUpload:
    """Holds `IN_FLIGHT_UPLOADS` up for the life of one request body.

    A context manager rather than a decrement at the end of the handler, because
    the interesting case is the one that does *not* reach the end: a client that
    disconnects mid-PUT raises out of the stream loop, and a hand-written
    decrement on the happy path only would leak the gauge upward on exactly the
    failures you wrote it to notice.
    """

    __slots__ = ()

    def __enter__(self) -> InFlightUpload:
        IN_FLIGHT_UPLOADS.inc()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        IN_FLIGHT_UPLOADS.dec()
