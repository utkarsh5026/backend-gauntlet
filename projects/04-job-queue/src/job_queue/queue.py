"""V1 — The claim engine: enqueue + the `SKIP LOCKED` dequeue, from scratch.

This is the piece you'd normally get from a broker (RabbitMQ / SQS / Sidekiq).
`enqueue` is a plain `INSERT`; the learning is in :meth:`Queue.claim`, the
**atomic dequeue** that hands each job to exactly one worker even when N workers
race.

The trap is the read-then-write race: `SELECT … LIMIT 1` then `UPDATE`
double-dispatches, because two workers read the same row before either claims it.
The fix is to select and claim in **one** statement —
`SELECT … FOR UPDATE SKIP LOCKED` (so a second worker steps over rows the first
already locked instead of blocking on them).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from . import metrics, scheduler
from .job import Job, JobId, NewJob

__all__ = ["Queue"]

log = structlog.get_logger(__name__)

_JOB_COLUMNS = """
    id,
    queue,
    kind,
    payload,
    state,
    attempts,
    max_attempts,
    run_at,
    locked_until,
    last_error,
    created_at
"""
"""The column list every read returns, single-sourced so `SELECT`, the claim's
`RETURNING`, and :func:`_to_job` can never drift apart."""


def _to_job(row: asyncpg.Record) -> Job:
    """Build a :class:`Job` from a row.

    `model_validate` rather than `Job(**row)` so the `state` text column is checked
    against :class:`~job_queue.job.JobState` on the way in — an unknown state is a
    corrupt row, and it should fail here rather than three frames later.
    """
    return Job.model_validate(dict(row))


class Queue:
    """Handle to the `jobs` table — the public surface of the V1 claim engine.

    A thin wrapper over an asyncpg pool plus the fallback `max_attempts` for jobs
    that don't set their own. One instance backs both the request handlers and
    every worker; there is nothing per-worker in it.
    """

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], default_max_attempts: int) -> None:
        self.pool = pool
        self.default_max_attempts = default_max_attempts

    async def enqueue(self, new: NewJob) -> JobId:
        """Insert a new job and return its freshly allocated id.

        A plain `INSERT` — the row lands in state `ready`. `max_attempts` falls back
        to the queue default when the request omits it, and `delay_secs` is clamped
        to `>= 0` and added to `now()` to compute `run_at`, so a job can be
        scheduled into the future but never into the past. Until `run_at` is due,
        :meth:`claim` won't hand the job out.

        The `NOTIFY` afterwards is V4's wakeup (see :mod:`job_queue.scheduler`). It
        is deliberately *best-effort*: a failed notify is logged and swallowed,
        because the durable table is the source of truth and the poll fallback will
        find the job anyway. Letting a notify failure fail the enqueue would trade a
        correctness-neutral optimisation for a 500.
        """
        max_attempts = (
            new.max_attempts if new.max_attempts is not None else self.default_max_attempts
        )
        delay = max(new.delay_secs or 0, 0)
        run_at = datetime.now(UTC) + timedelta(seconds=delay)

        job_id: JobId | None = await self.pool.fetchval(
            """
            INSERT INTO jobs (queue, kind, payload, max_attempts, run_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            new.queue,
            new.kind,
            new.payload,
            max_attempts,
            run_at,
        )
        if job_id is None:  # pragma: no cover - RETURNING id always yields a row
            raise RuntimeError("INSERT … RETURNING id produced no row")

        try:
            await scheduler.notify_ready(self.pool, new.queue)
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("NOTIFY failed; poll will catch up", error=str(exc), queue=new.queue)

        metrics.ENQUEUED_TOTAL.labels(queue=new.queue).inc()
        return job_id

    async def claim(self, queue: str, worker_id: str, limit: int, visibility: float) -> list[Job]:
        """Atomically claim up to `limit` due jobs from `queue` for `worker_id`.

        The heart of the vertical. In a **single statement** it selects the oldest
        `ready`, due (`run_at <= now()`) rows with `FOR UPDATE SKIP LOCKED` and
        flips them to `running`, stamping `locked_by = worker_id` and a lease
        `locked_until = now() + visibility`.

        Doing the select and the claim as one statement is what makes it safe under
        concurrency: a second worker steps over rows this call already locked
        instead of racing to re-read them, so no job is ever dispatched twice. Split
        it into a `SELECT` then an `UPDATE` and the read-then-write race is back.

        `visibility` is the lease window in seconds: if the worker crashes before
        acking, the reaper (V2) reclaims the job once `locked_until` passes.
        Returns the claimed jobs, already in state `running`; an empty list means
        nothing was due.
        """
        rows: list[asyncpg.Record] = await self.pool.fetch(
            f"""
            UPDATE jobs
            SET
                state = 'running',
                locked_by = $1,
                locked_at = now(),
                locked_until = now() + make_interval(secs => $2::double precision)
            WHERE id IN (
                SELECT id
                FROM jobs
                WHERE queue = $3 AND state = 'ready' AND run_at <= now()
                ORDER BY run_at
                FOR UPDATE SKIP LOCKED
                LIMIT $4
            )
            RETURNING {_JOB_COLUMNS}
            """,
            worker_id,
            float(visibility),
            queue,
            limit,
        )

        if not rows:
            metrics.CLAIMS_EMPTY_TOTAL.labels(queue=queue).inc()

        return [_to_job(row) for row in rows]

    async def ack(self, job_id: JobId) -> None:
        """Mark a job `done` and release its lease — the worker's success path.

        Clears `locked_by` / `locked_at` / `locked_until` so the job is retired and
        never claimed again. A no-op if no row has that id.
        """
        await self.pool.execute(
            """
            UPDATE jobs
            SET state = 'done', locked_by = NULL, locked_at = NULL, locked_until = NULL
            WHERE id = $1
            """,
            job_id,
        )

    async def get(self, job_id: JobId) -> Job | None:
        """Fetch a job by id, or `None` if no such job exists.

        The read-only lookup behind `GET /jobs/{id}`: a missing id maps to `None`
        (a 404), any state maps to the full row.
        """
        row: asyncpg.Record | None = await self.pool.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = $1", job_id
        )
        return _to_job(row) if row is not None else None

    async def get_dlq(self, limit: int, offset: int) -> list[Job]:
        """List dead-lettered jobs, newest-first, one page at a time.

        Ordered by `id DESC` — a monotonic, stable key, so `OFFSET` pages don't
        overlap or skip. (`updated_at` is *not* maintained past insert, so it can't
        serve as a "died at" sort; `id DESC` is the honest newest-first proxy.)

        `LIMIT`/`OFFSET` is fine for an admin-facing DLQ that's rarely huge; the
        tradeoff is that a deep `OFFSET` re-scans all skipped rows (O(offset)),
        where a keyset (`WHERE id < $last_seen`) would not. Not worth the complexity
        here. The caller's `limit`/`offset` arrive already clamped at the HTTP
        boundary (see `routes.get_dlq`) — this method trusts them.
        """
        rows: list[asyncpg.Record] = await self.pool.fetch(
            f"""
            SELECT {_JOB_COLUMNS}
            FROM jobs
            WHERE state = 'dead'
            ORDER BY id DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        return [_to_job(row) for row in rows]

    async def requeue(self, job_id: JobId) -> Job | None:
        """Return a **dead** job to `ready` with a fresh attempt budget.

        The `AND state = 'dead'` guard is load-bearing: it is what stops this admin
        door resurrecting a `running` job into a concurrent second execution, or
        re-running a `done` one. Anything that isn't dead — including an unknown id
        — comes back `None`, which the route maps to 404.
        """
        row: asyncpg.Record | None = await self.pool.fetchrow(
            f"""
            UPDATE jobs
            SET state = 'ready', attempts = 0, run_at = now()
            WHERE id = $1 AND state = 'dead'
            RETURNING {_JOB_COLUMNS}
            """,
            job_id,
        )
        if row is None:
            return None

        job = _to_job(row)
        try:
            await scheduler.notify_ready(self.pool, job.queue)
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("NOTIFY failed; poll will catch up", error=str(exc), queue=job.queue)
        return job

    async def count_by_state(self, queue: str) -> dict[str, Any]:
        """Depth per state for one queue — the admin/dashboard read behind the gauges."""
        rows: list[asyncpg.Record] = await self.pool.fetch(
            "SELECT state, COUNT(*) AS count FROM jobs WHERE queue = $1 GROUP BY state",
            queue,
        )
        return {row["state"]: row["count"] for row in rows}
