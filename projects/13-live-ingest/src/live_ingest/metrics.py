"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that registry at
`/metrics` — so declaring a metric here is all the wiring there is.

The series the SPEC grades, and what each is *for* against the Latency Wall:

* `PUBLISHERS_TOTAL{outcome}` / `ACTIVE_PUBLISHERS` — who got in and who the
  stream-key gate turned away. A climbing `rejected` is someone guessing keys.
* `BYTES_INGESTED_TOTAL` / `INGEST_BITRATE_BPS` — what the publisher is pushing.
  A bitrate that sags while the publisher claims 6 Mbps is your parser falling
  behind, and the socket's read buffer pushing back on its encoder.
* `PARTS_PRODUCED_TOTAL` / `SEGMENTS_PRODUCED_TOTAL` — the fan-out proof. The
  boss fight asks you to show each part is muxed **once** regardless of viewer
  count; this counter, next to `VIEWER_REQUESTS_TOTAL{kind="part"}`, is that
  proof. If it scales with viewers, you are muxing per request.
* `BLOCKING_RELOADS_TOTAL{outcome}` / `HELD_REQUESTS` — V4's health in a few
  numbers. `held` ≈ `served` and `timed_out` ≈ 0 is the design working;
  `timed_out` climbing means the publisher or your cut cadence stalled.
* `PACKAGING_SECONDS` — how long one part took to build. It is latency added to
  every viewer before they could even ask for the part.
* `LIVE_EDGE_AGE_SECONDS` — now minus the newest part's presentation time. The
  glass-to-glass proxy you can compute with no player cooperation, and the one
  to alert on.

Four conventions worth copying rather than re-deriving:

**`_total` is not doubled.** `prometheus_client` strips a trailing `_total` from
a Counter's name and the exposition format puts it back, so the names below
export exactly as written.

**Counters per outcome, never a pre-computed ratio.** The boss fight grades "≥ 95%
of blocking reloads served the requested part first time", but there is no
ratio gauge here. A ratio cannot be aggregated or re-windowed; its counters can,
and Prometheus divides at query time.

**Buckets are a decision.** The defaults top out at 10 s and are sparse under
100 ms, which is exactly where a 300 ms part target and a "within ~1 part" hold
time live. A histogram whose buckets do not bracket your target cannot fail.

**No stream key as a label.** It is the obvious label, and it is both a
credential and a cardinality bomb. Per-stream detail belongs in a log line with
a hashed key — which is what the checklist asks for.

Wiring the graded *call sites* is the observability horizontal item and is left
for you. The ingest server bumps `RTMP_CONNECTIONS_TOTAL` and
`RTMP_SESSIONS_ENDED_TOTAL` because they are how you see the scaffold is alive at
all; everything else is declared here and placed by you as V1–V4 land.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ACTIVE_PUBLISHERS",
    "BLOCKING_RELOADS_TOTAL",
    "BYTES_INGESTED_TOTAL",
    "HELD_REQUESTS",
    "HOLD_SECONDS",
    "INGEST_BITRATE_BPS",
    "LIVE_EDGE_AGE_SECONDS",
    "PACKAGING_SECONDS",
    "PARTS_PRODUCED_TOTAL",
    "PUBLISHERS_TOTAL",
    "RTMP_CONNECTIONS_TOTAL",
    "RTMP_SESSIONS_ENDED_TOTAL",
    "SEGMENTS_PRODUCED_TOTAL",
    "VIEWER_REQUESTS_TOTAL",
    "preregister",
]

PART_BUCKETS = (0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.200, 0.300, 0.500, 1.0, 5.0)
"""Dense below one part duration (~300 ms) — where packaging time and blocking
hold time are graded — with 5 s at the top for the blocking-reload bound."""

# --- ingest (RTMP) ---
RTMP_CONNECTIONS_TOTAL = Counter(
    "live_ingest_rtmp_connections_total",
    "TCP connections accepted on the RTMP port",
)
RTMP_SESSIONS_ENDED_TOTAL = Counter(
    "live_ingest_rtmp_sessions_ended_total",
    "RTMP sessions ended, by why",
    ["reason"],
)
PUBLISHERS_TOTAL = Counter(
    "live_ingest_publishers_total",
    "Publish attempts, by outcome of the stream-key gate",
    ["outcome"],
)
ACTIVE_PUBLISHERS = Gauge("live_ingest_active_publishers", "Sessions currently publishing")
BYTES_INGESTED_TOTAL = Counter("live_ingest_bytes_ingested_total", "RTMP bytes read")
INGEST_BITRATE_BPS = Gauge("live_ingest_ingest_bitrate_bps", "Recent ingest bitrate (bits/sec)")

# --- packaging (V3) ---
PARTS_PRODUCED_TOTAL = Counter("live_ingest_parts_produced_total", "LL-HLS parts built")
SEGMENTS_PRODUCED_TOTAL = Counter("live_ingest_segments_produced_total", "Segments closed")
PACKAGING_SECONDS = Histogram(
    "live_ingest_packaging_seconds",
    "Time to build one part",
    buckets=PART_BUCKETS,
)
LIVE_EDGE_AGE_SECONDS = Gauge(
    "live_ingest_live_edge_age_seconds",
    "Now minus the newest part's presentation time — the glass-to-glass proxy",
)

# --- delivery (V4) ---
VIEWER_REQUESTS_TOTAL = Counter(
    "live_ingest_viewer_requests_total",
    "Delivery requests, by kind",
    ["kind"],
)
BLOCKING_RELOADS_TOTAL = Counter(
    "live_ingest_blocking_reloads_total",
    "Blocking playlist reloads, by outcome",
    ["outcome"],
)
HELD_REQUESTS = Gauge("live_ingest_held_requests", "Blocking reloads currently parked")
HOLD_SECONDS = Histogram(
    "live_ingest_hold_seconds",
    "How long a blocking reload was held",
    buckets=PART_BUCKETS,
)

SESSION_END_REASONS = ("closed", "protocol_error", "unimplemented", "error", "shutdown")
"""Every way a session ends, enumerated so each exports at zero. `unimplemented`
is the scaffold's worklist made scrapeable: it climbs by one per connection
until the vertical the session reached is written."""


def preregister() -> None:
    """Create every known labelled child so it exports at zero from startup.

    A labelled metric does not exist until something creates that child, so
    without this `live_ingest_blocking_reloads_total{outcome="timed_out"}` is
    **absent** until the first timeout — and absent is not zero. `rate()` over
    it returns no data, and an alert written against it never fires on exactly
    the day it should.
    """
    for reason in SESSION_END_REASONS:
        RTMP_SESSIONS_ENDED_TOTAL.labels(reason=reason)
    for outcome in ("accepted", "rejected"):
        PUBLISHERS_TOTAL.labels(outcome=outcome)
    for kind in ("playlist", "init", "segment", "part"):
        VIEWER_REQUESTS_TOTAL.labels(kind=kind)
    for outcome in ("immediate", "held", "timed_out"):
        BLOCKING_RELOADS_TOTAL.labels(outcome=outcome)
