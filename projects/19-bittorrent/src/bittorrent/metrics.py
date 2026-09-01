"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry
at `/metrics` — so declaring a metric here is all the wiring there is. That
replaces the Rust side's install-a-global-recorder step entirely: no `install()`
to call, no ordering constraint against telemetry init, and no window at startup
where a metric is silently dropped.

The series the SPEC grades, and what each is *for* during the boss fight:

* `BYTES_DOWNLOADED_TOTAL` / `BYTES_UPLOADED_TOTAL` — the two halves of the
  share ratio, and the only honest measure of whether the swarm is moving.
* `PIECES_VERIFIED_TOTAL` — labelled `result=ok|failed`. **A rising `failed` is a
  lying peer**, and it is the difference between "my download is slow" and "peer
  X has sent me forty bad blocks".
* `PEERS_CONNECTED` — bounded by `MAX_PEERS`.
* `PEERS_UNCHOKED` — **the gauge that proves the V6 slot cap**. The boss fight's
  central claim is that this never exceeds `UPLOAD_SLOTS + 1` no matter how many
  peers connect, and this is the number that either shows it or does not.
* `ANNOUNCES_TOTAL` — labelled `transport=http|udp`, `result=ok|error`, so one
  dead tracker is visible as a rate rather than inferred from a log.
* `TIME_TO_FIRST_BLOCK` — a histogram, because the boss fight grades its **p99**
  and a mean would hide exactly the starvation the optimistic unchoke exists to
  prevent.

Three conventions worth copying rather than re-deriving:

**Counters per outcome, never a pre-computed ratio.** There is no
`pieces_verified_ratio` gauge here on purpose. A ratio cannot be aggregated
across replicas or re-windowed after the fact; two counters can, and Prometheus
divides at query time.

**Buckets are a decision.** `prometheus_client`'s default histogram buckets jump
0.1 -> 0.25 -> 0.5, and the boss fight's target is p99 time-to-first-block
<= 250 ms — landing exactly on a default bucket edge, where a 110 ms result and a
249 ms result are indistinguishable. The buckets below are dense where the
target is. A histogram whose buckets do not bracket your SLO is a metric that
cannot fail.

**No peer address as a label.** It is the obvious next label and it is a
cardinality bomb — the boss fight alone would mint fifty series per metric, and
a public swarm is unbounded. It is also the SPEC's security item: a metric
endpoint that enumerates who is in the swarm deanonymizes every one of them. Peer
identity belongs in a log line with a retention policy, not in a time series.

Wiring the *call sites* — bumping these from `peer.py`, `tracker.py`,
`download.py` and `seeder.py` — is the observability horizontal item and is left
for you. This module declares them and single-sources the names.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ANNOUNCES_TOTAL",
    "BYTES_DOWNLOADED_TOTAL",
    "BYTES_UPLOADED_TOTAL",
    "PEERS_CONNECTED",
    "PEERS_UNCHOKED",
    "PIECES_VERIFIED_TOTAL",
    "TIME_TO_FIRST_BLOCK",
    "TTFB_BUCKETS",
]

TTFB_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
"""Seconds. Dense around the boss fight's 250 ms p99 target — which is itself a
bucket edge, so the criterion is answerable directly rather than by interpolating
across a gap. Loopback time-to-first-block is single-digit milliseconds when
nothing is contended, so the low end matters too: a run that comes back all in
the first bucket is telling you the storm never actually formed."""

# ---- Counters (rates) ------------------------------------------------------

BYTES_DOWNLOADED_TOTAL = Counter(
    "bt_bytes_downloaded_total",
    "Payload bytes downloaded and verified",
)

BYTES_UPLOADED_TOTAL = Counter(
    "bt_bytes_uploaded_total",
    "Payload bytes uploaded to peers",
)

PIECES_VERIFIED_TOTAL = Counter(
    "bt_pieces_verified_total",
    "Piece SHA-1 verifications; a rising failed count is a lying peer",
    ["result"],
)

ANNOUNCES_TOTAL = Counter(
    "bt_tracker_announces_total",
    "Tracker announces by transport and outcome",
    ["transport", "result"],
)

# ---- Histograms (distributions) --------------------------------------------

TIME_TO_FIRST_BLOCK = Histogram(
    "bt_time_to_first_block_seconds",
    "Seconds from a peer connecting to its first piece block — the starvation signal",
    buckets=TTFB_BUCKETS,
)

# ---- Gauges (current state) ------------------------------------------------

PEERS_CONNECTED = Gauge(
    "bt_peers_connected",
    "Currently-connected peers, inbound and outbound; bounded by MAX_PEERS",
)

PEERS_UNCHOKED = Gauge(
    "bt_peers_unchoked",
    "Currently-unchoked peers — this is what proves the V6 upload-slot cap",
)
