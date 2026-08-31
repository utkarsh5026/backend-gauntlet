"""V4 — Scheduling + LISTEN/NOTIFY: low pickup latency without busy-polling.

Delayed jobs already "work" via V1 (the claim filters `run_at <= now()`), so a
polling worker eventually picks them up. The problem V4 solves is the
latency-vs-load tradeoff polling forces: poll fast and you flood an idle DB with
empty `SELECT`s; poll slow and every job waits.

The fix is Postgres `LISTEN`/`NOTIFY`: `enqueue` (and a retry/delay coming due)
issues a `NOTIFY` on the queue's channel; idle workers `LISTEN` and wake the
instant work appears — with a slow poll as the fallback. `NOTIFY` is
fire-and-forget and not durable, so the poll fallback is **not optional**: it keeps
the durable table the source of truth and the notify a mere optimization.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
import asyncpg.pool

__all__ = ["channel_name", "notify_ready", "wait_for_work"]


def channel_name(queue: str) -> str:
    """The Postgres `LISTEN`/`NOTIFY` channel name for a given queue.

    Namespacing by queue keeps `NOTIFY`s scoped: a wakeup meant for one queue must
    never wake a worker parked on another, or every queue would share one wakeup
    and workers would thrash on each other's traffic.
    """
    return f"jobs_{queue}"


async def wait_for_work(
    pool: asyncpg.Pool[asyncpg.Record], queue: str, poll_fallback: float
) -> None:
    """Park until a `NOTIFY` arrives on `queue`'s channel, or the sleep elapses.

    The low-latency alternative to a tight polling loop: an idle worker calls this
    instead of immediately re-querying the table.

    Three things bound the sleep, and each exists for a reason:

    * **`poll_fallback`** — because `NOTIFY` is fire-and-forget. A notification
      sent while nobody is listening, or lost on a connection blip, is simply gone,
      so the caller must still wake on a bounded cadence and re-check the durable
      table. This is what makes the notify an optimisation rather than a
      correctness dependency.
    * **the earliest `ready` job's `run_at`** — because nothing issues a `NOTIFY`
      at the moment a *delayed* job comes due. `enqueue` notified when the row was
      inserted, which was too early to claim it. Without this bound a job delayed
      one second behind a ten-second poll strands for the full ten.
    * **the notify itself** — the fast path, when work appears right now.

    Returns once any of them fires; callers re-query the queue afterwards rather
    than branching on which one it was.

    Note it holds a pooled connection for the duration of the wait — `LISTEN` is
    connection state, so there is no way around that. It is why `db_pool_max` has
    to comfortably exceed `worker_concurrency`: every idle worker is holding one
    connection open, and a pool sized to the worker count alone would leave the API
    with nothing to claim.
    """
    channel = channel_name(queue)
    woken = asyncio.Event()

    def _on_notify(
        _conn: asyncpg.Connection[asyncpg.Record]
        | asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
        _pid: int,
        _channel: str,
        _payload: object,
        /,
    ) -> None:
        """asyncpg dispatches this on the event loop, so setting the flag is enough.

        The payload is ignored by design: the notify carries no job data, because
        the durable table is the source of truth and the worker goes back to it.
        """
        woken.set()

    async with pool.acquire() as conn:
        await conn.add_listener(channel, _on_notify)
        try:
            next_due: datetime | None = await conn.fetchval(
                """
                SELECT run_at
                FROM jobs
                WHERE queue = $1 AND state = 'ready'
                ORDER BY run_at
                LIMIT 1
                """,
                queue,
            )

            sleep_for = poll_fallback
            if next_due is not None:
                until_due = (next_due - datetime.now(UTC)).total_seconds()
                sleep_for = min(poll_fallback, max(until_due, 0.0))

            try:
                async with asyncio.timeout(sleep_for):
                    await woken.wait()
            except TimeoutError:
                pass
        finally:
            await conn.remove_listener(channel, _on_notify)


async def notify_ready(pool: asyncpg.Pool[asyncpg.Record], queue: str) -> None:
    """Send a `NOTIFY` on `queue`'s channel to wake workers parked in
    :func:`wait_for_work`.

    Called whenever new work becomes visible sooner than a worker's next poll would
    find it — `enqueue`, or an admin requeue. The notify is a payload-less
    optimization: it carries no job data (workers still go back to the table to
    find it) and it is fine if nobody is listening.

    Uses the `pg_notify()` function rather than the `NOTIFY` statement because the
    channel is a *value* here. `NOTIFY` takes an identifier, which cannot be a bound
    parameter — writing it would mean interpolating a caller-influenced name into
    SQL text. `pg_notify($1, '')` keeps the channel a parameter, which is why
    `queue` names are charset-validated in `job.py` and never concatenated here.
    """
    await pool.fetchval("SELECT pg_notify($1, '')", channel_name(queue))
