"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` - so declaring a metric here is all the wiring there is. The three
the SPEC grades:

* :data:`REDIRECTS_TOTAL` - redirects served, labelled by cache outcome.
* :data:`CACHE_LOOKUPS_TOTAL` - every resolution, labelled by outcome; `hit`
  over the sum is the cache hit ratio.
* :data:`INGEST_QUEUE_DEPTH` - live depth of the click-ingestion queue.

Note the shape of the first two: a *counter per outcome*, not a pre-computed
ratio. A ratio cannot be aggregated across replicas or re-windowed after the
fact; two counters can, and Prometheus does the division at query time.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

__all__ = ["CACHE_LOOKUPS_TOTAL", "INGEST_QUEUE_DEPTH", "REDIRECTS_TOTAL"]

REDIRECTS_TOTAL = Counter(
    "url_shortener_redirects_total",
    "Redirects served (3xx returned), labelled by cache outcome",
    ["cache"],
)

CACHE_LOOKUPS_TOTAL = Counter(
    "url_shortener_cache_lookups_total",
    "Cache-aside resolutions on the redirect path, labelled by outcome",
    ["outcome"],
)

INGEST_QUEUE_DEPTH = Gauge(
    "url_shortener_ingest_queue_depth",
    "Buffered clicks waiting in the ingestion queue",
)
