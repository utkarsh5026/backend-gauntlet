"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely: no `install()`
to call from `main`, no ordering constraint against telemetry init, and no
window at startup where a metric silently goes nowhere.

The series the SPEC grades, and what each is *for* during the Crowded Room:

* `RTP_RECEIVED_TOTAL` / `RTP_FORWARDED_TOTAL` — **the fan-out amplification**,
  and the one number that says what an SFU is. Forwarded ÷ received is the
  subscriber count; if it is not, either subscribers are not connected or you
  are dropping. The boss fight's first criterion is this ratio holding at ≥ 50.
* `RTP_DROPPED_TOTAL` — labelled by `reason`, because the reasons mean opposite
  things. `not_selected` is the SFU working correctly (a deselected simulcast
  layer). `no_route` is a peer sending media before ICE completed — or someone
  spraying your open port. `inbox_full` is Python-specific and the one to watch:
  it means the event loop could not drain the media queue fast enough, which is
  a GIL/allocation finding, not a network one.
* `STUN_MESSAGES_TOTAL` / `ICE_NOMINATED_TOTAL` — V1 made visible. A browser
  that never connects, with STUN messages climbing and nominations flat, is an
  integrity or XOR-MAPPED-ADDRESS bug and nothing else.
* `LAYER_SWITCHES_TOTAL` — labelled `dir=up|down`. A rate that never settles is
  the flapping the SPEC asks you to design hysteresis against; the boss fight's
  sagging profile should produce a small, countable number of switches.
* `KEYFRAME_REQUESTS_TOTAL` — one PLI per pending up-switch. If this tracks the
  *packet* rate rather than the switch rate, the keyframe-owed flag is not
  clearing (V3's criterion, observable from a graph).
* `FORWARDING_SECONDS` — ingress packet to egress `sendto`, the boss fight's
  p99 ≤ 10 ms.
* `ESTIMATED_BITRATE_BPS` / `SELECTED_BITRATE_BPS` — V4 next to V3: what the
  estimator believes, and what the selector did about it. Watching a subscriber
  adapt in real time is these two lines converging.

Four conventions worth copying rather than re-deriving:

**`_total` is not doubled.** `prometheus_client` strips a trailing `_total` from
a Counter's name and the exposition format puts it back, so `Counter(
"sfu_rtp_received_total")` exports exactly `sfu_rtp_received_total`. Naming them
without the suffix is what produces the wrong series here.

**Counters per outcome, never a pre-computed ratio.** There is no
`sfu_fanout_ratio` gauge, deliberately. A ratio cannot be aggregated across
instances or re-windowed after the fact; two counters can, and Prometheus
divides at query time.

**Buckets are a decision.** `prometheus_client`'s defaults jump
0.005 -> 0.01 -> 0.025, which straddles the 10 ms target so coarsely that a
6 ms p99 and a 24 ms p99 land two buckets apart with nothing in between. The
buckets below are dense from 100 µs to 20 ms, where this SLO actually lives. A
histogram whose buckets do not bracket your target is a metric that cannot fail.

**No SSRC, peer id or address as a label.** They are the obvious next labels and
they are a cardinality bomb: the boss fight alone mints fifty subscribers, each
with its own outbound SSRC, and a public deployment is unbounded. Per-subscriber
detail belongs in a log line with a retention policy, not in a time series —
which is why `ESTIMATED_BITRATE_BPS` is a single gauge for the busiest
subscriber rather than one series per peer.

Wiring the *call sites* for the adaptation metrics — bumping `LAYER_SWITCHES`
from the selector, `NACKS_TRANSLATED` from the rewriter, observing
`FORWARDING_SECONDS` around the fan-out — is the observability horizontal item
and is left for you. This module declares them and single-sources the names.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "BYTES_FORWARDED_TOTAL",
    "ESTIMATED_BITRATE_BPS",
    "FORWARDING_SECONDS",
    "ICE_NOMINATED_TOTAL",
    "KEYFRAME_REQUESTS_TOTAL",
    "LAYER_SWITCHES_TOTAL",
    "NACKS_TRANSLATED_TOTAL",
    "PEERS",
    "ROOMS",
    "RTP_DROPPED_TOTAL",
    "RTP_FORWARDED_TOTAL",
    "RTP_RECEIVED_TOTAL",
    "SELECTED_BITRATE_BPS",
    "STUN_MESSAGES_TOTAL",
    "preregister",
]

FORWARDING_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.002,
    0.004,
    0.006,
    0.008,
    0.010,
    0.015,
    0.020,
    0.050,
    float("inf"),
)
"""Dense either side of the boss fight's 10 ms p99 — see the module docstring."""

# --- topology (gauges) ---
ROOMS = Gauge("sfu_rooms", "Active rooms")
PEERS = Gauge("sfu_peers", "Connected peers", ["role"])

# --- forwarding volume (counters) ---
RTP_RECEIVED_TOTAL = Counter("sfu_rtp_received_total", "RTP packets received from publishers")
RTP_FORWARDED_TOTAL = Counter("sfu_rtp_forwarded_total", "RTP packets forwarded to subscribers")
BYTES_FORWARDED_TOTAL = Counter("sfu_bytes_forwarded_total", "Media-plane bytes forwarded")
RTP_DROPPED_TOTAL = Counter(
    "sfu_rtp_dropped_total",
    "RTP packets dropped before forwarding",
    ["reason"],
)
FORWARDING_SECONDS = Histogram(
    "sfu_forwarding_seconds",
    "Ingress RTP packet to egress sendto",
    buckets=FORWARDING_BUCKETS,
)

# --- ICE / STUN (counters) ---
STUN_MESSAGES_TOTAL = Counter("sfu_stun_messages_total", "STUN messages processed", ["kind"])
ICE_NOMINATED_TOTAL = Counter("sfu_ice_nominated_total", "ICE candidate pairs nominated")

# --- adaptation (counters) ---
LAYER_SWITCHES_TOTAL = Counter("sfu_layer_switches_total", "Simulcast layer switches", ["dir"])
KEYFRAME_REQUESTS_TOTAL = Counter(
    "sfu_keyframe_requests_total",
    "Keyframe (PLI/FIR) requests sent upstream",
)
NACKS_TRANSLATED_TOTAL = Counter(
    "sfu_nacks_translated_total",
    "NACKs translated subscriber<->publisher",
)

# --- quality (gauges) ---
ESTIMATED_BITRATE_BPS = Gauge("sfu_estimated_bitrate_bps", "Estimated downlink bitrate (bps)")
SELECTED_BITRATE_BPS = Gauge("sfu_selected_bitrate_bps", "Selected forwarded bitrate (bps)")

DROP_REASONS = ("no_route", "not_selected", "inbox_full", "malformed")
"""Every reason a packet is dropped, enumerated so each exports at zero."""


def preregister() -> None:
    """Create every known labelled child so it exports at zero from startup.

    A labelled Prometheus metric does not exist until something creates that
    child, so without this `sfu_rtp_dropped_total{reason="no_route"}` is simply
    **absent** until the first stray packet — and "absent" is not "zero". A
    dashboard shows a gap, `rate()` returns no data, an alert written as
    `rate(...) > 0` never fires, and one written as `absent(...)` fires
    constantly for the entirely healthy reason that nothing has gone wrong yet.

    Enumerating the label values is only possible because they are a closed set
    — four drop reasons, three STUN kinds, two roles, two directions. That is
    the same property that makes them safe as labels at all; see the module
    docstring on why an SSRC is not.
    """
    for reason in DROP_REASONS:
        RTP_DROPPED_TOTAL.labels(reason=reason)
    for kind in ("request", "response", "error"):
        STUN_MESSAGES_TOTAL.labels(kind=kind)
    for role in ("publisher", "subscriber"):
        PEERS.labels(role=role)
    for direction in ("up", "down"):
        LAYER_SWITCHES_TOTAL.labels(dir=direction)
