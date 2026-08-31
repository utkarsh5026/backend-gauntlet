"""V2 — Visibility timeout / lease: at-least-once delivery.

A claim isn't "this job is done" — it's "this worker may *try* for a while." Each
claim stamps `locked_until = now() + lease` (done in `queue.claim`). If the worker
acks before then, great. If it crashes, the job sits `running` with an expired
lease — and the **reaper** here returns it to `ready` so another worker retries it.
That sweep is the whole reason a crashed worker doesn't lose its job.

The cost you must accept: a worker can finish a job and die *before* acking, so the
job runs again. There is no free exactly-once — the answer is **idempotent
handlers**. The lease length is a real tradeoff (too short → spurious double-runs
of slow jobs; too long → slow crash recovery).
"""

from __future__ import annotations

import asyncio

import asyncpg
import structlog

from . import metrics

__all__ = ["reap_expired", "reap_loop"]

log = structlog.get_logger(__name__)


async def reap_expired(pool: asyncpg.Pool[asyncpg.Record]) -> int:
    """Return `running` jobs whose lease has passed to `ready`. Returns the count.

    The `state = 'running'` guard is as important as the clock one: a `done` job
    that still carries a stale `locked_until` must not be resurrected, and a `ready`
    job was never leased in the first place. Filtering on state *and* time is what
    makes this a lease reaper rather than a sweep that re-runs finished work.

    `RETURNING id` rather than reading asyncpg's `"UPDATE n"` status tag — the count
    is then the row count, with no command-tag string to parse.
    """
    rows: list[asyncpg.Record] = await pool.fetch(
        """
        UPDATE jobs
        SET state = 'ready', locked_by = NULL, locked_until = NULL
        WHERE state = 'running' AND locked_until < now()
        RETURNING id
        """
    )
    requeued = len(rows)
    if requeued:
        metrics.LEASES_REAPED_TOTAL.inc(requeued)
    return requeued


async def reap_loop(
    pool: asyncpg.Pool[asyncpg.Record], interval: float, shutdown: asyncio.Event
) -> None:
    """Sweep for expired leases every `interval` seconds until `shutdown` is set.

    The sleep is raced against the shutdown flag rather than slept through, so a
    stop is observed immediately instead of up to `interval` later. A failed sweep
    is logged and retried on the next tick: a transient DB hiccup must not take the
    reaper down, because without it every crashed worker's job stays stuck.
    """
    log.info("lease reaper started", interval_secs=interval)
    while not shutdown.is_set():
        try:
            async with asyncio.timeout(interval):
                await shutdown.wait()
            break
        except TimeoutError:
            pass
        try:
            requeued = await reap_expired(pool)
            if requeued:
                log.info("reaped expired job leases", requeued=requeued)
        except (OSError, asyncpg.PostgresError) as exc:
            log.error("reaper sweep failed", error=str(exc))
    log.debug("lease reaper shutting down")
