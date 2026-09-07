"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely: no `install()`
to call from `main`, no ordering constraint against telemetry init, and no
window at startup where a metric silently goes nowhere.

The series the SPEC grades, and what each is *for* during the Lossy Mile:

* `PACKETS_SENT_TOTAL{kind}` / `PACKETS_RECEIVED_TOTAL` — volume, and with
  `kind="retransmit"` split out, the cost of V3's recovery. Retransmits as a
  fraction of originals is bandwidth the congestion controller is not getting.
* `PACKETS_LOST_TOTAL` / `NACKS_TOTAL{dir}` / `RETRANSMITS_TOTAL` — the
  recovery loop, end to end. The boss fight's first criterion is a ratio over
  these: ≥ 90% of losses recovered before deadline. Note there is no
  `effective_loss` gauge — see "counters per outcome" below.
* `DUPLICATES_TOTAL` / `LATE_DROPS_TOTAL` — the two ways V2 discards a packet,
  and they mean opposite things. Duplicates are the network (or your own
  retransmit answering a NACK you had already given up on). Late drops are
  *you*: the playout deadline passed while the packet was in flight, which is
  either real path delay or a jitter target set too tight.
* `JITTER_SECONDS` / `BUFFER_DEPTH` / `ADDED_LATENCY_SECONDS` — the
  smoothness-versus-latency tradeoff, made watchable. The boss fight caps added
  latency at 150 ms and says it must not creep upward across the run; a
  gauge that only ever rises is the criterion failing in slow motion.
* `TARGET_BITRATE_BPS` — V4's belief about the path. Plotted against the
  `tc netem` cap, this is the convergence-and-recovery criterion as a picture.
* `PLAYOUT_SECONDS` — how long a frame took from arrival to release, the
  histogram behind "≥ 99.5% of frames played on time, no stall over 300 ms".

Four conventions worth copying rather than re-deriving:

**`_total` is not doubled.** `prometheus_client` strips a trailing `_total`
from a Counter's name and the exposition format puts it back, so
`Counter("media_transport_packets_sent_total")` exports exactly that. Naming
them without the suffix is what produces the wrong series here.

**Counters per outcome, never a pre-computed ratio.** There is deliberately no
`effective_loss_ratio` gauge, even though the boss fight is graded on one. A
ratio cannot be aggregated across instances or re-windowed after the fact; the
counters it is made of can, and Prometheus divides at query time.

**Buckets are a decision.** `prometheus_client`'s defaults top out at 10 s,
which is useless for a 300 ms stall criterion — a 40 ms playout and a 900 ms
playout land in adjacent buckets. The buckets below are dense from 1 ms to
500 ms, where this SLO actually lives. A histogram whose buckets do not bracket
your target is a metric that cannot fail.

**No SSRC as a label.** It is the obvious next label and it is a cardinality
bomb: an SSRC is a random 32-bit number minted per stream, so every reconnect
mints a new series that never goes away. Per-stream detail belongs in a log
line with a retention policy — which is exactly what the observability
checklist asks for, a tracing context per SSRC, not a metric per SSRC.

Wiring most of these *call sites* is the observability horizontal item and is
left for you: `session.py` bumps the volume counters because it must (they are
how you see the scaffold is alive at all), but jitter, buffer depth, added
latency, playout timing and the loss/recovery counters are yours to place as
you build V2, V3 and V4. This module declares them and single-sources the names.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ADDED_LATENCY_SECONDS",
    "BUFFER_DEPTH",
    "BYTES_RECEIVED_TOTAL",
    "BYTES_SENT_TOTAL",
    "DATAGRAMS_DROPPED_TOTAL",
    "DUPLICATES_TOTAL",
    "JITTER_SECONDS",
    "LATE_DROPS_TOTAL",
    "NACKS_TOTAL",
    "PACKETS_LOST_TOTAL",
    "PACKETS_RECEIVED_TOTAL",
    "PACKETS_SENT_TOTAL",
    "PLAYOUT_SECONDS",
    "RETRANSMITS_TOTAL",
    "TARGET_BITRATE_BPS",
    "preregister",
]

LATENCY_BUCKETS = (
    0.001,
    0.005,
    0.010,
    0.025,
    0.050,
    0.075,
    0.100,
    0.150,
    0.200,
    0.300,
    0.500,
    float("inf"),
)
"""Dense across the 100 ms playout target, the 150 ms added-latency cap and the
300 ms stall bound — the three numbers the boss fight is graded on."""

# --- volume (counters) ---
PACKETS_SENT_TOTAL = Counter(
    "media_transport_packets_sent_total",
    "RTP packets sent",
    ["kind"],
)
PACKETS_RECEIVED_TOTAL = Counter(
    "media_transport_packets_received_total",
    "RTP packets received, before jitter-buffer admission",
)
BYTES_SENT_TOTAL = Counter("media_transport_bytes_sent_total", "Media-plane bytes sent")
BYTES_RECEIVED_TOTAL = Counter("media_transport_bytes_received_total", "Media-plane bytes received")

# --- loss & recovery (counters) ---
PACKETS_LOST_TOTAL = Counter(
    "media_transport_packets_lost_total",
    "Sequence gaps detected at the receiver",
)
NACKS_TOTAL = Counter("media_transport_nacks_total", "NACK feedback packets", ["dir"])
RETRANSMITS_TOTAL = Counter(
    "media_transport_retransmits_total",
    "Packets resent from the retransmit cache",
)
DUPLICATES_TOTAL = Counter(
    "media_transport_duplicates_total",
    "Duplicate packets discarded by the jitter buffer",
)
LATE_DROPS_TOTAL = Counter(
    "media_transport_late_drops_total",
    "Packets dropped for arriving after their playout deadline",
)
DATAGRAMS_DROPPED_TOTAL = Counter(
    "media_transport_datagrams_dropped_total",
    "Datagrams discarded before parsing",
    ["reason"],
)

# --- quality (gauges + histograms) ---
JITTER_SECONDS = Gauge(
    "media_transport_jitter_seconds",
    "Smoothed interarrival jitter estimate (RFC 3550)",
)
BUFFER_DEPTH = Gauge("media_transport_jitter_buffer_depth", "Jitter-buffer depth, in packets")
ADDED_LATENCY_SECONDS = Gauge(
    "media_transport_added_latency_seconds",
    "Playout latency the jitter buffer is currently adding",
)
TARGET_BITRATE_BPS = Gauge(
    "media_transport_target_bitrate_bps",
    "Congestion-control target send rate (bits/sec)",
)
PLAYOUT_SECONDS = Histogram(
    "media_transport_playout_seconds",
    "Arrival to playout release, per frame",
    buckets=LATENCY_BUCKETS,
)

DROP_REASONS = ("inbox_full", "oversized", "malformed", "unexpected_ssrc")
"""Every reason a datagram is dropped before it reaches a parser, enumerated so
each exports at zero. `unexpected_ssrc` is the security checklist's source
validation; `inbox_full` is Python-specific and the one to watch — see `udp.py`.
"""


def preregister() -> None:
    """Create every known labelled child so it exports at zero from startup.

    A labelled Prometheus metric does not exist until something creates that
    child, so without this `media_transport_nacks_total{dir="sent"}` is simply
    **absent** until the first NACK — and "absent" is not "zero". A dashboard
    shows a gap, `rate()` returns no data, an alert written as `rate(...) > 0`
    never fires, and one written as `absent(...)` fires constantly for the
    entirely healthy reason that nothing has gone wrong yet.

    Enumerating the label values is only possible because they are a closed set
    — four drop reasons, two directions, two send kinds. That is the same
    property that makes them safe as labels at all; see the module docstring on
    why an SSRC is not.
    """
    for reason in DROP_REASONS:
        DATAGRAMS_DROPPED_TOTAL.labels(reason=reason)
    for direction in ("sent", "received"):
        NACKS_TOTAL.labels(dir=direction)
    for kind in ("original", "retransmit"):
        PACKETS_SENT_TOTAL.labels(kind=kind)
