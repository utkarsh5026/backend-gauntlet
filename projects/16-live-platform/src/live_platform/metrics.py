"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register into the default registry at import
time, and `common_telemetry.metrics_routes()` renders that registry at
`/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely.

Two of these matter beyond dashboards. `TRANSCODE_DESIRED_REPLICAS` (fed from
`WorkerPool.desired_replicas`) is what the HPA scales the worker Deployment on
(V2), and `GLASS_TO_GLASS` is the latency the boss fight judges.

Conventions worth copying rather than re-deriving:

**Seconds, not milliseconds.** The Rust named the latency histogram
`live_glass_to_glass_ms`. Prometheus' convention is base units — seconds — so
that every latency on a dashboard composes with every other without a unit
conversion hiding in a query. It is `live_glass_to_glass_seconds` here, and
SPEC.md's Proof line was updated to match.

**Buckets bracket the target.** The boss fight asks for glass-to-glass p95
≤ 3 s. `prometheus_client`'s default buckets would put 2.5 s and 5 s in adjacent
buckets, and `histogram_quantile` cannot tell you whether you passed. The
buckets below are dense across 1–4 s, where that SLO lives.

**Counters per outcome, never a ratio.** There is no `edge_hit_ratio` gauge even
though the boss fight grades one. A ratio cannot be summed across pods or
re-windowed after the fact; `EDGE_REQUESTS{outcome}` can, and Prometheus divides
at query time.

**No stream key or pod name as a label.** A stream key is a secret and an
unbounded set; a pod name churns with every HPA scale event. Both are
cardinality bombs, and Prometheus attaches the pod label on scrape anyway.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "CHAT_CONNECTIONS",
    "CHAT_FANOUT",
    "CHAT_SLOW_DROPS",
    "EDGE_ORIGIN_FILLS",
    "EDGE_REQUESTS",
    "GLASS_TO_GLASS",
    "STREAMS_LIVE",
    "STREAM_TRANSITIONS",
    "TRANSCODE_DESIRED_REPLICAS",
    "TRANSCODE_JOBS",
    "TRANSCODE_QUEUE_DEPTH",
    "preregister",
]

# --- control plane (V1) ---
STREAMS_LIVE = Gauge("live_streams", "Streams currently live (packaged and playable)")
STREAM_TRANSITIONS = Counter(
    "live_stream_transitions_total", "Stream state transitions, by the state entered", ["to"]
)

# --- transcode workers (V2) ---
TRANSCODE_QUEUE_DEPTH = Gauge(
    "live_transcode_queue_depth", "Transcode jobs waiting to be claimed (the HPA's input)"
)
TRANSCODE_DESIRED_REPLICAS = Gauge(
    "live_transcode_desired_replicas", "Replicas the worker pool asks the HPA to scale to"
)
TRANSCODE_JOBS = Counter(
    "live_transcode_jobs_total", "Transcode jobs finished, by result", ["result"]
)

# --- edge delivery (V3) ---
EDGE_REQUESTS = Counter(
    "live_edge_requests_total", "Edge segment/playlist requests, by outcome", ["outcome"]
)
EDGE_ORIGIN_FILLS = Counter(
    "live_edge_origin_fills_total", "Origin fills issued on a miss (single-flight keeps this low)"
)

# --- chat (V4) ---
CHAT_CONNECTIONS = Gauge("live_chat_connections", "Open chat WebSocket connections on this pod")
CHAT_FANOUT = Counter("live_chat_fanout_total", "Chat messages delivered into subscriber outboxes")
CHAT_SLOW_DROPS = Counter(
    "live_chat_slow_drops_total", "Chat subscribers shed by the slow-consumer policy"
)

# --- end to end ---
GLASS_TO_GLASS = Histogram(
    "live_glass_to_glass_seconds",
    "Glass-to-glass latency: capture timestamp to playable at the edge",
    buckets=(0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 7.5, 10.0),
)

TRANSITION_TARGETS = ("ingesting", "transcoding", "live", "ended")
"""Every state a transition can *enter*. `offline` is absent on purpose: nothing
transitions into it, a stream starts there. A test holds this in step with
`StreamState` — kept as literals so this module imports no vertical."""


def preregister() -> None:
    """Create every labelled child at zero, so the series exist before traffic.

    A `rate()` over an absent series returns no data, and an alert over no data
    never fires — so "the edge has never coalesced a request" should read as a
    zero on the dashboard, not as a gap. Idempotent: `labels()` returns the
    existing child on repeat calls.
    """
    for to in TRANSITION_TARGETS:
        STREAM_TRANSITIONS.labels(to=to)
    for result in ("ok", "retried", "failed"):
        TRANSCODE_JOBS.labels(result=result)
    for outcome in ("hit", "miss", "coalesced"):
        EDGE_REQUESTS.labels(outcome=outcome)
