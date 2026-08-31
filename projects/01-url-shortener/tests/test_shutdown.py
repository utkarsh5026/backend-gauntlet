"""Graceful shutdown - the bounded final flush.

No Postgres needed: what is under test is the *budget*, so the pool is a
stand-in whose behaviour (fast, wedged, or exploding) is the input.
"""

from __future__ import annotations

import asyncio
import time
from typing import cast

import asyncpg

from url_shortener.ingest import ClickEvent, ClickIngestor
from url_shortener.shutdown import ShutdownOutcome, drain_ingestor


class _FastPool:
    async def execute(self, query: str, *args: object) -> str:
        _ = (query, args)
        return "INSERT 0 1"


class _WedgedPool:
    """A database that accepted the statement and never answered."""

    async def execute(self, query: str, *args: object) -> str:
        _ = (query, args)
        await asyncio.sleep(3600)
        return "INSERT 0 1"


class _ExplodingPool:
    async def execute(self, query: str, *args: object) -> str:
        _ = (query, args)
        raise RuntimeError("simulated flush failure")


def _pool(stub: object) -> asyncpg.Pool[asyncpg.Record]:
    return cast("asyncpg.Pool[asyncpg.Record]", stub)


async def test_a_clean_flush_reports_flushed() -> None:
    ingestor = ClickIngestor(_pool(_FastPool()), flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())
    ingestor.sink.accept(ClickEvent(link_id=1))

    assert await drain_ingestor(ingestor, task, budget=5.0) is ShutdownOutcome.FLUSHED
    assert task.done()


async def test_a_wedged_flush_gives_up_at_the_budget() -> None:
    """A stuck write must not hold the process past the orchestrator's
    SIGTERM->SIGKILL deadline. We would rather lose one batch of analytics than
    be killed mid-flush.

    The 50ms budget against a one-hour write is the proof that the budget is
    what returns, not the task.
    """
    ingestor = ClickIngestor(_pool(_WedgedPool()), flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())
    ingestor.sink.accept(ClickEvent(link_id=1))

    started = time.perf_counter()
    outcome = await drain_ingestor(ingestor, task, budget=0.05)
    elapsed = time.perf_counter() - started

    assert outcome is ShutdownOutcome.TIMED_OUT
    assert elapsed < 1.0, "returned near the budget, not after the wedged write"
    assert task.cancelled() or task.cancelling()


async def test_a_broken_flush_does_not_break_shutdown() -> None:
    """An exception on the way out must still let the process exit cleanly."""
    ingestor = ClickIngestor(_pool(_ExplodingPool()), flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())
    ingestor.sink.accept(ClickEvent(link_id=1))

    assert await drain_ingestor(ingestor, task, budget=5.0) is ShutdownOutcome.FAILED


async def test_draining_an_idle_ingestor_is_clean() -> None:
    ingestor = ClickIngestor(_pool(_FastPool()), flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())

    assert await drain_ingestor(ingestor, task, budget=5.0) is ShutdownOutcome.FLUSHED
