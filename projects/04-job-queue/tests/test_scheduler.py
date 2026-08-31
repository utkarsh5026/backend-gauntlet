"""V4 — LISTEN/NOTIFY: waking an idle worker without busy-polling.

These are timing tests, so each one gives itself a generous ceiling and asserts on
the *shape* of the timing (woke early / didn't wake early) rather than on a precise
duration — otherwise they'd flake on a loaded CI box.
"""

from __future__ import annotations

import asyncio

import asyncpg

from job_queue.scheduler import channel_name, notify_ready, wait_for_work

from .conftest import unique_queue


def test_channel_name_is_stable_and_queue_scoped() -> None:
    assert channel_name("default") == "jobs_default"
    assert channel_name("emails") == "jobs_emails"
    assert channel_name("a") != channel_name("b")


async def test_notify_wakes_an_idle_listener(pg_pool: asyncpg.Pool[asyncpg.Record]) -> None:
    """The fast path: a NOTIFY wakes a worker parked behind a 30s poll fallback.

    If this hangs, the notify is doing nothing and pickup latency has silently
    become the poll interval.
    """
    queue_name = unique_queue("wake")
    waiter = asyncio.create_task(wait_for_work(pg_pool, queue_name, 30.0))
    await asyncio.sleep(0.3)  # let the LISTEN land before notifying

    await notify_ready(pg_pool, queue_name)

    async with asyncio.timeout(5):
        await waiter


async def test_poll_fallback_returns_without_any_notify(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """The durable fallback: with **no** NOTIFY at all, `wait_for_work` still
    returns after roughly the poll interval.

    This is what makes NOTIFY a mere optimisation. A notification sent while nobody
    was listening, or dropped on a connection blip, is simply lost — and a queue
    whose only wakeup path is lossy would strand jobs forever.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    await wait_for_work(pg_pool, unique_queue("fallback"), 0.3)
    elapsed = loop.time() - start

    assert elapsed >= 0.25, f"returned in {elapsed:.3f}s — too early to be the poll fallback"
    assert elapsed < 3.0, f"returned in {elapsed:.3f}s — the poll fallback never fired"


async def test_notify_is_scoped_to_its_queue_channel(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """A NOTIFY on queue *B* must not wake a worker listening on queue *A*.

    If it did, every queue would share one wakeup and workers would thrash on each
    other's traffic. A's only legitimate wakeup here is its own 1s poll, so waking
    much earlier means the channels are leaking.
    """
    queue_a = unique_queue("scope_a")
    queue_b = unique_queue("scope_b")

    loop = asyncio.get_running_loop()
    start = loop.time()
    waiter = asyncio.create_task(wait_for_work(pg_pool, queue_a, 1.0))

    await asyncio.sleep(0.15)
    await notify_ready(pg_pool, queue_b)  # a different queue — A must ignore it

    async with asyncio.timeout(5):
        await waiter
    elapsed = loop.time() - start

    assert elapsed >= 0.9, f"A woke after {elapsed:.3f}s — a NOTIFY for B leaked into A"


async def test_notify_ready_is_fire_and_forget_without_listeners(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Notifying an empty channel is not an error — nobody listening is normal."""
    await notify_ready(pg_pool, unique_queue("no_listener"))


async def test_wait_returns_when_a_delayed_job_comes_due(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """The due-aware bound: nothing issues a NOTIFY at the moment a *delayed* job
    becomes claimable, so the idle sleep must be bounded by the earliest `ready`
    job's `run_at`. Without it, a job delayed 0.5s behind a 30s poll strands for 30s.
    """
    from job_queue.queue import Queue

    from .conftest import new_job

    queue_name = unique_queue("due")
    queue = Queue(pg_pool, 5)
    await queue.enqueue(new_job(queue_name, delay_secs=1))

    loop = asyncio.get_running_loop()
    start = loop.time()
    await wait_for_work(pg_pool, queue_name, 30.0)
    elapsed = loop.time() - start

    assert elapsed < 5.0, (
        f"waited {elapsed:.3f}s for a job due in 1s — the sleep is not bounded by run_at, "
        "so a delayed job strands behind the poll fallback"
    )
