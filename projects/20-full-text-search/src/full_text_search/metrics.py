"""Prometheus metrics for the observability checklist.

Wiring, not a vertical: this module defines the series and nothing else. Putting
the *call sites* in the right places — that is the graded horizontal item.

`prometheus_client` works differently from the Rust `metrics` facade the Rust
scaffold used. There, call sites named a metric with a string and a global
recorder resolved it, so a metric that was never registered was simply a no-op.
Here a metric is an **object**, created once at import and registered into the
default registry as a side effect of construction. Two consequences you will
meet:

*   Creating the same metric name twice raises `ValueError: Duplicated
    timeseries`. That is why these are module-level singletons and why nothing
    constructs a metric inside a function or a class `__init__`.
*   Because they are objects, importing this module is enough to make the series
    appear at `/metrics` with a zero value. That is a feature: a counter that
    only appears after the first event is a counter you cannot alert on.

`common_telemetry.metrics_routes()` renders the default registry, so there is
nothing to install — the names below are the whole contract.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "DOCS_INDEXED",
    "MERGES",
    "QUERY_CACHE_LOOKUPS",
    "SEARCHES",
    "SEARCH_DURATION",
    "SEGMENTS",
]

DOCS_INDEXED = Counter(
    "search_documents_indexed_total",
    "Documents accepted for indexing (across all shards).",
)

SEARCHES = Counter(
    "search_searches_total",
    "Searches served (a cache hit still counts as a search).",
)

SEARCH_DURATION = Histogram(
    "search_duration_seconds",
    "End-to-end search latency, in seconds.",
    # The boss fight grades p99 <= 50ms, so the buckets have to be dense around
    # there — the default prometheus_client buckets jump 0.05 -> 0.075 -> 0.1 and
    # would put the entire interesting range in two buckets. A histogram can only
    # resolve a quantile as finely as its bucket edges allow.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0),
)

QUERY_CACHE_LOOKUPS = Counter(
    "search_query_cache_lookups_total",
    "Query-cache lookups. Hit ratio is hit / (hit + miss).",
    labelnames=("outcome",),
)

SEGMENTS = Gauge(
    "search_segments",
    "Live segments in a shard. Climbs on refresh, drops on merge.",
    labelnames=("shard",),
)

MERGES = Counter(
    "search_merges_total",
    "Segment merges completed (V4).",
)
