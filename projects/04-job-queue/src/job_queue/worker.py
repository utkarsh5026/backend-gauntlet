"""The worker runtime: the loop that drains the queue.

This is wiring — it *calls into* the verticals (claim = V1, lease via the reaper =
V2, retry/DLQ = V3, wakeup = V4) and ties them into a lifecycle: claim a batch →
run each job → ack on success / nack on failure → repeat.

Workers run only when `RUN_WORKERS=true` (see `main`), so the API can be served
without a worker pool attached to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import structlog

from . import handlers, metrics, retry, scheduler
from .job import Job
from .queue import Queue
from .retry import Disposition, RetryPolicy

__all__ = ["WorkerConfig", "process_one", "run"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class WorkerConfig:
    """Per-worker tuning, shared by every worker in the pool."""

    queue_name: str = "default"
    """Which named queue this worker drains."""

    poll_interval: float = 1.0
    """How long an idle worker waits before re-checking, in seconds. V4's
    LISTEN/NOTIFY makes this the *fallback* cadence, not the pickup latency."""

    visibility_timeout: float = 30.0
    """Lease length stamped on each claimed job (V2), in seconds."""

    claim_batch: int = 10
    """How many jobs to claim per round-trip."""

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    """Backoff policy for failed jobs (V3)."""

    log_dir: Path = Path("logs")
    """Where `exec`/`shell` jobs write their per-attempt output."""


async def _wait_for_work_or_shutdown(
    queue: Queue, cfg: WorkerConfig, worker_id: str, shutdown: asyncio.Event
) -> None:
    """Park on the V4 wakeup, but return immediately if shutdown is signalled.

    Racing the two rather than waiting on the notify alone is what keeps a stop
    responsive: an idle worker parked on a ten-second poll fallback would otherwise
    take ten seconds to notice it should exit.
    """
    work = asyncio.create_task(
        scheduler.wait_for_work(queue.pool, cfg.queue_name, cfg.poll_interval)
    )
    stop = asyncio.create_task(shutdown.wait())
    done, pending = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if work in done:
        exc = work.exception()
        if exc is not None:
            log.error("wait_for_work failed", worker=worker_id, error=str(exc))


async def run(worker_id: str, queue: Queue, cfg: WorkerConfig, shutdown: asyncio.Event) -> None:
    """Drive one worker: claim → run → ack/nack in a loop until shutdown.

    Each iteration claims a batch of up to `cfg.claim_batch` jobs (V1) and hands
    each to :func:`process_one`. When a claim comes back empty the worker parks on
    the LISTEN/NOTIFY wakeup (V4) until work arrives or shutdown is set.

    Claim errors are logged and retried on the next tick rather than raised, so a
    transient DB hiccup doesn't kill the worker — a worker that exits on the first
    blip is worse than one that retries, because nothing restarts it.

    Shutdown is checked *between* jobs, so a stop drains at most one more job rather
    than abandoning an in-flight one. That is the deliberate trade: finishing the
    job in hand costs a moment of shutdown latency and saves a duplicate execution
    (the alternative — abandoning it — leaves the row `running` until its lease
    expires, and then someone runs it again).
    """
    log.info("worker started", worker=worker_id, queue=cfg.queue_name)

    while not shutdown.is_set():
        try:
            claimed = await queue.claim(
                cfg.queue_name, worker_id, cfg.claim_batch, cfg.visibility_timeout
            )
        except (OSError, asyncpg.PostgresError) as exc:
            log.error("claim failed", worker=worker_id, error=str(exc))
            claimed = []

        if not claimed:
            await _wait_for_work_or_shutdown(queue, cfg, worker_id, shutdown)
            continue

        for job in claimed:
            if shutdown.is_set():
                break
            await process_one(queue, cfg, worker_id, job)

    log.info("worker stopped", worker=worker_id)


async def process_one(queue: Queue, cfg: WorkerConfig, worker_id: str, job: Job) -> None:
    """Run one job and record its outcome.

    The bound logger carries the SPEC's observability trio (`job_id`, `kind`,
    `attempt`) plus `worker`, so every line this attempt emits is queryable without
    the payload ever being logged — payloads may carry secrets, and the `exec`/
    `shell` kinds make that concrete.

    Every exception is caught, not just :class:`~job_queue.handlers.JobFailed`: a
    bug in a handler must become a nacked job, never a dead worker. The message
    becomes `last_error`, and V3 decides between a backoff retry and the DLQ.
    """
    bound = log.bind(worker=worker_id, job_id=job.id, kind=job.kind, attempt=job.attempts)
    started = time.perf_counter()

    try:
        await handlers.dispatch(job, cfg.log_dir)
    except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the worker
        elapsed = time.perf_counter() - started
        metrics.EXECUTION_SECONDS.labels(kind=job.kind).observe(elapsed)
        await _nack(queue, cfg, job, exc, bound)
        return

    elapsed = time.perf_counter() - started
    metrics.EXECUTION_SECONDS.labels(kind=job.kind).observe(elapsed)

    try:
        await queue.ack(job.id)
    except (OSError, asyncpg.PostgresError) as exc:
        # The job ran but the ack didn't land, so its lease will expire and someone
        # will run it again. This is precisely the at-least-once cost V2 names, and
        # the reason handlers have to be idempotent.
        bound.error("ack failed", outcome="ack_failed", error=str(exc))
        return

    metrics.COMPLETED_TOTAL.labels(kind=job.kind).inc()
    end_to_end = max((datetime.now(UTC) - job.created_at).total_seconds(), 0.0)
    metrics.END_TO_END_LATENCY_SECONDS.labels(kind=job.kind).observe(end_to_end)
    bound.info("job done", outcome="done", elapsed_ms=round(elapsed * 1000, 2))


async def _nack(
    queue: Queue,
    cfg: WorkerConfig,
    job: Job,
    exc: Exception,
    bound: structlog.stdlib.BoundLogger,
) -> None:
    """Record a failed attempt and let V3 choose retry-with-backoff or the DLQ."""
    reason = str(exc) or type(exc).__name__
    try:
        disposition = await retry.nack(queue.pool, cfg.retry, job, reason)
    except (OSError, asyncpg.PostgresError) as nack_exc:
        bound.error("nack failed", outcome="nack_failed", error=str(nack_exc))
        return

    if disposition is Disposition.RETRIED:
        bound.warning("job failed; scheduled for retry", outcome="retried", error=reason)
    else:
        bound.error("job exhausted retries; dead-lettered", outcome="dead_lettered", error=reason)
