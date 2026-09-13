"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry at
`/metrics` — so declaring a metric here is all the wiring there is. That replaces the
Rust side's install-a-global-recorder step entirely.

The series the SPEC grades:

* `TRANSFERS_TOTAL` — transfers, labelled `result = ok|rejected|conflict|exhausted`;
  `rate()` over it is transfers/sec.
* `SERIALIZATION_RETRIES_TOTAL` — conflict retries: how hard V2 is fighting.
* `IDEMPOTENCY_LOOKUPS_TOTAL` — labelled `outcome = hit|miss`. The hit ratio is
  `hit / (hit + miss)`, computed at query time.
* `WEBHOOK_DELIVERIES_TOTAL` — labelled `state = delivered|failed|dead`.
* `WEBHOOK_OUTBOX_LAG_SECONDS` — age of the oldest still-pending outbox event.

Plus one the boss fight leans on: `TRANSFER_DURATION_SECONDS`, the server-side view
of the p99 ≤ 25 ms target.

Note what is *not* here: an `idempotency_hit_ratio` gauge. Counters per outcome,
never a pre-computed ratio — a ratio can't be aggregated across replicas or
re-windowed after the fact; two counters can, and Prometheus divides.

Wiring the *call sites* (the transfer path, the idempotency store, the dispatcher)
is the Observability checklist item. This module declares the series and makes
`/metrics` render them.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "IDEMPOTENCY_LOOKUPS_TOTAL",
    "SERIALIZATION_RETRIES_TOTAL",
    "TRANSFERS_TOTAL",
    "TRANSFER_DURATION_SECONDS",
    "WEBHOOK_DELIVERIES_TOTAL",
    "WEBHOOK_OUTBOX_LAG_SECONDS",
    "init",
]

TRANSFER_RESULTS = ("ok", "rejected", "conflict", "exhausted")
IDEMPOTENCY_OUTCOMES = ("hit", "miss")
DELIVERY_STATES = ("delivered", "failed", "dead")

# ---- Counters (rates) ------------------------------------------------------

TRANSFERS_TOTAL = Counter(
    "ledger_transfers_total",
    "Transfers attempted, labelled by result",
    ["result"],
)

SERIALIZATION_RETRIES_TOTAL = Counter(
    "ledger_serialization_retries_total",
    "Transfers retried after a serialization conflict (V2 contention)",
)

IDEMPOTENCY_LOOKUPS_TOTAL = Counter(
    "ledger_idempotency_lookups_total",
    "Idempotency key lookups; hit ratio = hit / (hit + miss)",
    ["outcome"],
)

WEBHOOK_DELIVERIES_TOTAL = Counter(
    "ledger_webhook_deliveries_total",
    "Webhook delivery attempts resolved, labelled delivered|failed|dead",
    ["state"],
)

# ---- Gauges (current state) ------------------------------------------------

WEBHOOK_OUTBOX_LAG_SECONDS = Gauge(
    "ledger_webhook_outbox_lag_seconds",
    "Age of the oldest pending webhook outbox event, in seconds",
)
"""Set by the dispatcher each round with `set`, not `set_function`: the value lives
in Postgres, and a scrape-time callback can't await a query."""

# ---- Histograms (distributions) --------------------------------------------

TRANSFER_DURATION_SECONDS = Histogram(
    "ledger_transfer_duration_seconds",
    "Server-side time to settle one transfer, retries included",
    # The boss fight grades p99 ≤ 25ms, so the buckets need resolution *around*
    # 25ms; a histogram whose nearest edges are 10ms and 100ms cannot tell you
    # whether you passed.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.05, 0.1, 0.25, 1.0),
)


def init() -> None:
    """Pre-create every known label set, so each series exists at zero from the
    first scrape.

    A labelled collector renders *nothing* until some label combination has been
    used: `prometheus_client` creates a child on the first `.labels(...)` call. On a
    freshly started ledger that means `ledger_webhook_deliveries_total{state="dead"}`
    is simply absent — so an alert on its `rate()` can't fire, and you find out about
    the DLQ during the incident rather than before it. Idempotent: `.labels()`
    returns the existing child on a repeat call.
    """
    for result in TRANSFER_RESULTS:
        TRANSFERS_TOTAL.labels(result=result)
    for outcome in IDEMPOTENCY_OUTCOMES:
        IDEMPOTENCY_LOOKUPS_TOTAL.labels(outcome=outcome)
    for state in DELIVERY_STATES:
        WEBHOOK_DELIVERIES_TOTAL.labels(state=state)
