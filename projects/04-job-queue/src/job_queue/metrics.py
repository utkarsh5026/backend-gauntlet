"""Prometheus metrics for the observability checklist.

`prometheus_client` collectors register themselves into the default registry at
import time, and `common_telemetry.metrics_routes()` renders that same registry at
`/metrics` — so declaring a metric here is all the wiring there is. That replaces
the Rust side's install-a-global-recorder step entirely; there is no `install()`
to call and no ordering constraint against telemetry init.

Three shapes, three jobs:

* **Counters** — rates. Enqueued / completed / retried / dead-lettered / leases
  reaped / empty claims. Note these are *counters per outcome*, never a
  pre-computed ratio: a ratio cannot be aggregated across replicas or re-windowed
  after the fact, two counters can, and Prometheus does the division at query time.
* **Gauges** — current state of the `jobs` table. Not tied to an event, so they are
  sampled on a timer by :func:`gauge_loop`.
* **Histograms** — distributions: handler execution time and end-to-end latency.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import structlog
from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "CLAIMS_EMPTY_TOTAL",
    "COMPLETED_TOTAL",
    "DEAD_LETTERED_TOTAL",
    "DLQ_DEPTH",
    "END_TO_END_LATENCY_SECONDS",
    "ENQUEUED_TOTAL",
    "EXECUTION_SECONDS",
    "LEASES_REAPED_TOTAL",
    "OLDEST_READY_AGE_SECONDS",
    "READY_DEPTH",
    "RETRIED_TOTAL",
    "RUNNING_DEPTH",
    "gauge_loop",
    "sample_gauges",
]

log = structlog.get_logger(__name__)

# ---- Counters (rates) ------------------------------------------------------

ENQUEUED_TOTAL = Counter("job_queue_enqueued_total", "Jobs successfully enqueued", ["queue"])

COMPLETED_TOTAL = Counter("job_queue_completed_total", "Jobs acked done by a worker", ["kind"])

RETRIED_TOTAL = Counter("job_queue_retried_total", "Failures rescheduled with backoff", ["kind"])

DEAD_LETTERED_TOTAL = Counter(
    "job_queue_dead_lettered_total",
    "Failures that exhausted max_attempts and landed in the DLQ",
    ["kind"],
)

LEASES_REAPED_TOTAL = Counter(
    "job_queue_leases_reaped_total",
    "Expired leases returned to ready by the reaper",
)
"""A non-zero reap rate means workers are dying *or* the lease is too short."""

CLAIMS_EMPTY_TOTAL = Counter(
    "job_queue_claims_empty_total", "Claims that found no due, ready work", ["queue"]
)
"""High-frequency empty claims on an idle queue is exactly the busy-poll cost V4's
LISTEN/NOTIFY exists to avoid — so this is the metric that proves that win."""

# ---- Gauges (current state) ------------------------------------------------

READY_DEPTH = Gauge("job_queue_ready_depth", "Current count of ready jobs", ["queue"])

RUNNING_DEPTH = Gauge(
    "job_queue_running_depth", "Current count of running (leased) jobs", ["queue"]
)

DLQ_DEPTH = Gauge("job_queue_dlq_depth", "Current count of dead-lettered jobs", ["queue"])

OLDEST_READY_AGE_SECONDS = Gauge(
    "job_queue_oldest_ready_age_seconds",
    "Age in seconds of the oldest due ready job — the queue lag signal",
    ["queue"],
)
"""*The* lag metric: a steady depth can hide a queue that is steadily falling
behind, and this cannot. Reported as `0` (not absent) when there is no ready
backlog, so a scrape can't confuse "no data yet" with "caught up"."""

# ---- Histograms (distributions) -------------------------------------------

EXECUTION_SECONDS = Histogram(
    "job_queue_execution_seconds", "Job handler execution time, seconds", ["kind"]
)

END_TO_END_LATENCY_SECONDS = Histogram(
    "job_queue_end_to_end_latency_seconds", "Enqueue-to-done latency, seconds", ["kind"]
)


async def sample_gauges(pool: asyncpg.Pool[asyncpg.Record], queue: str) -> None:
    """Read the current state of `queue` out of the table and publish the gauges.

    Two statements, not four: one `GROUP BY state` covers all three depths, and one
    aggregate covers the lag. The lag query filters `run_at <= now()` on purpose —
    a job scheduled for next week is not "lag", and counting it as such would make
    the one metric that says "you are falling behind" cry wolf on every delayed job.
    """
    depths: list[Any] = await pool.fetch(
        """
        SELECT state, COUNT(*) AS count
        FROM jobs
        WHERE queue = $1
        GROUP BY state
        """,
        queue,
    )
    by_state: dict[str, int] = {row["state"]: row["count"] for row in depths}

    lag: float | None = await pool.fetchval(
        """
        SELECT COALESCE(EXTRACT(EPOCH FROM (now() - MIN(run_at))), 0)::double precision
        FROM jobs
        WHERE queue = $1 AND state = 'ready' AND run_at <= now()
        """,
        queue,
    )

    READY_DEPTH.labels(queue=queue).set(by_state.get("ready", 0))
    RUNNING_DEPTH.labels(queue=queue).set(by_state.get("running", 0))
    DLQ_DEPTH.labels(queue=queue).set(by_state.get("dead", 0))
    OLDEST_READY_AGE_SECONDS.labels(queue=queue).set(lag or 0.0)


async def gauge_loop(
    pool: asyncpg.Pool[asyncpg.Record],
    queue: str,
    interval: float,
    shutdown: asyncio.Event,
) -> None:
    """Sample the gauges every `interval` seconds until `shutdown` is set.

    Same shape as `lease.reap_loop`: race the sleep against the shutdown flag so a
    stop is observed immediately rather than at the end of the current interval. A
    failed sample is logged and retried on the next tick — losing a gauge sample is
    not a reason to take the sampler down.
    """
    log.info("gauge sampler started", interval_secs=interval)
    while not shutdown.is_set():
        try:
            async with asyncio.timeout(interval):
                await shutdown.wait()
            break
        except TimeoutError:
            pass
        try:
            await sample_gauges(pool, queue)
        except (OSError, asyncpg.PostgresError) as exc:
            log.error("gauge sample failed", error=str(exc))
    log.debug("gauge sampler shutting down")
