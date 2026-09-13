"""Prometheus metrics for the observability checklist — **wired**, not a vertical.

`prometheus_client` collectors register into the default registry at import
time and `common_telemetry.metrics_routes()` renders that registry at `/metrics`,
so declaring a metric here is all the wiring there is. Bumping them at the right
moments inside V1–V4 is the observability checklist item, and it is yours.

The series the SPEC grades, and what each is *for* during the Hairpin:

* `RELAY_COPIES_OUT_TOTAL{src,dst}` — **the fan-out amplification**, and the
  number the boss fight's first criterion reads. For each remote region it must
  track *layers demanded there*, not subscribers there: doubling Frankfurt's
  viewers adds zero to `{src="eu-west",dst="ap-south"}`'s rate. Labelled by the
  region *pair* rather than just the destination so that a scrape of three SFUs
  aggregates without anyone joining on `instance`.
* `RELAY_DROPPED_TOTAL{reason}` — `loop` is the loop guard doing its job and
  should read zero on a correct mesh with honest peers; `unknown_peer` is someone
  spraying the backbone port; `truncated` is the framing bounds check.
* `NODE_ROLE` / `NODE_TERM` / `ELECTIONS_TOTAL` — the partition timeline. During
  the 60 s backbone sag, exactly one node should read `role=2` at any instant.
* `LEG_LAYERS{peer}` — the per-leg layer-set trace: a mobile-only region reads 1.
* `KEYFRAME_REQUESTS_TOTAL` — one per up-switch. If it tracks the *packet* rate,
  the keyframe-owed flag is not clearing (V3).
* `FORWARD_SECONDS` — ingress packet to egress `sendto`, the per-region p99
  ≤ 10 ms target. Observed around the fan-out once project 15's pump is mounted.
* `DATAGRAMS_DROPPED_TOTAL{plane,reason="inbox_full"}` — Python-specific and the
  one to watch under load: the event loop could not drain a socket's queue. That
  is a GIL/allocation finding for the profile, not a network one.

Conventions worth copying rather than re-deriving:

**`_total` is not doubled.** `prometheus_client` strips a trailing `_total` from a
Counter's name and the exposition format puts it back.

**Counters per outcome, never a precomputed ratio.** No `relay_amplification`
gauge — a ratio cannot be summed across three SFUs or re-windowed later.

**Seconds, not milliseconds.** Prometheus base units. The Rust named this
`_latency_ms`; a histogram in ms is a dashboard that disagrees with every other
latency panel by 1000×.

**No room id, SSRC or peer address as a label.** Rooms are unbounded (the
cardinality bomb); regions are not — they come from `PEERS`, a closed set, which
is the only reason they are safe as labels here.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ACTIVE_REGIONS",
    "DATAGRAMS_DROPPED_TOTAL",
    "DEMAND_CHANGES_TOTAL",
    "ELECTIONS_TOTAL",
    "FORWARD_SECONDS",
    "KEYFRAME_REQUESTS_TOTAL",
    "LEG_LAYERS",
    "NODE_ROLE",
    "NODE_TERM",
    "PLACEMENT_COMMITS_TOTAL",
    "RECORDED_BYTES_TOTAL",
    "RECORDINGS_ACTIVE",
    "RECORDING_SEGMENTS_TOTAL",
    "RELAY_BYTES_OUT_TOTAL",
    "RELAY_COPIES_IN_TOTAL",
    "RELAY_COPIES_OUT_TOTAL",
    "RELAY_DROPPED_TOTAL",
    "RELAY_LEGS",
    "ROOMS_PLACED",
    "preregister",
]

FORWARD_BUCKETS = (
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
"""Dense either side of the 10 ms p99 target. The library defaults jump
0.005 -> 0.01 -> 0.025, so a 6 ms and a 24 ms p99 would be indistinguishable."""

# --- placement / consensus (V1) ---
ROOMS_PLACED = Gauge("conf_rooms_placed", "Rooms with a committed home region")
ACTIVE_REGIONS = Gauge("conf_active_regions", "Active regions summed across placed rooms")
PLACEMENT_COMMITS_TOTAL = Counter(
    "conf_placement_commits_total",
    "Replicated-log entries committed",
    ["kind"],
)
ELECTIONS_TOTAL = Counter("conf_elections_total", "Leader elections started by this node")
NODE_ROLE = Gauge("conf_node_role", "Placement role: 0 follower, 1 candidate, 2 leader")
NODE_TERM = Gauge("conf_node_term", "Current placement term")

# --- cascade transport (V2) ---
RELAY_LEGS = Gauge("conf_relay_legs", "Open backbone relay legs", ["peer"])
RELAY_COPIES_OUT_TOTAL = Counter(
    "conf_relay_copies_out_total",
    "Relay copies sent on the backbone, by region pair",
    ["src", "dst"],
)
RELAY_COPIES_IN_TOTAL = Counter(
    "conf_relay_copies_in_total",
    "Relay copies received from a peer SFU, by region pair",
    ["src", "dst"],
)
RELAY_BYTES_OUT_TOTAL = Counter(
    "conf_relay_bytes_out_total",
    "Bytes sent across the backbone, by region pair",
    ["src", "dst"],
)
RELAY_DROPPED_TOTAL = Counter(
    "conf_relay_dropped_total",
    "Relay datagrams dropped",
    ["reason"],
)

# --- cross-region routing (V3) ---
LEG_LAYERS = Gauge("conf_leg_layers", "Simulcast layers carried on a backbone leg", ["peer"])
KEYFRAME_REQUESTS_TOTAL = Counter(
    "conf_keyframe_requests_total",
    "Keyframe (PLI/FIR) requests sent upstream on a region's up-switch",
)
DEMAND_CHANGES_TOTAL = Counter(
    "conf_demand_changes_total",
    "Leg recomputations that changed a leg's carried layer set",
)

# --- recording (V4) ---
RECORDINGS_ACTIVE = Gauge("conf_recordings_active", "Active recordings")
RECORDED_BYTES_TOTAL = Counter("conf_recorded_bytes_total", "Encoded RTP bytes recorded")
RECORDING_SEGMENTS_TOTAL = Counter(
    "conf_recording_segments_total",
    "Recording segments finalized (closed + indexed)",
)

# --- the planes (wired) ---
DATAGRAMS_DROPPED_TOTAL = Counter(
    "conf_datagrams_dropped_total",
    "Datagrams shed at a UDP socket before any parser saw them",
    ["plane", "reason"],
)
FORWARD_SECONDS = Histogram(
    "conf_forward_seconds",
    "Per-region forwarding latency: ingress packet to egress sendto",
    buckets=FORWARD_BUCKETS,
)

PLACEMENT_ENTRY_KINDS = ("place_room", "region_interest")
RELAY_DROP_REASONS = ("loop", "unknown_peer", "no_route", "truncated")
PLANES = ("media", "cascade")
PLANE_DROP_REASONS = ("inbox_full", "oversized")


def preregister(region: str, peer_regions: Iterable[str]) -> None:
    """Create every known labelled child so it exports at zero from startup.

    A labelled series does not exist until something creates that child, and
    "absent" is not "zero": `rate()` over an absent series returns no data, so a
    dashboard shows a gap and an alert on it never fires. The label values are a
    closed set — including the region pairs, because `PEERS` is — which is what
    makes enumerating them possible at all.
    """
    for kind in PLACEMENT_ENTRY_KINDS:
        PLACEMENT_COMMITS_TOTAL.labels(kind=kind)
    for reason in RELAY_DROP_REASONS:
        RELAY_DROPPED_TOTAL.labels(reason=reason)
    for plane in PLANES:
        for reason in PLANE_DROP_REASONS:
            DATAGRAMS_DROPPED_TOTAL.labels(plane=plane, reason=reason)
    for peer in peer_regions:
        RELAY_LEGS.labels(peer=peer)
        LEG_LAYERS.labels(peer=peer)
        RELAY_COPIES_OUT_TOTAL.labels(src=region, dst=peer)
        RELAY_BYTES_OUT_TOTAL.labels(src=region, dst=peer)
        RELAY_COPIES_IN_TOTAL.labels(src=peer, dst=region)
