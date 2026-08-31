"""V3 - async click ingestion: bounded queue, explicit overflow, batching.

The batching proof does not need a database. What has to be true is that N
clicks become *one statement* with the values bound as parameters, and a
recording stand-in for the pool shows that directly - which is a stronger,
faster check than inferring it from rows landing in Postgres. The tests that do
use Postgres prove the rows actually arrive.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import cast

import asyncpg
import pytest

from url_shortener.ingest import ClickEvent, ClickIngestor
from url_shortener.metrics import INGEST_QUEUE_DEPTH

from .conftest import unique_slug


class RecordingPool:
    """Stands in for the connection pool, remembering every statement issued."""

    def __init__(self, fail: bool = False) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.fail = fail

    async def execute(self, query: str, *args: object) -> str:
        self.statements.append((query, args))
        if self.fail:
            raise asyncpg.PostgresError("simulated failure")
        return "INSERT 0 1"


def _fake_pool(pool: RecordingPool) -> asyncpg.Pool[asyncpg.Record]:
    """The stand-in, typed as a pool. `flush` only ever calls `execute`."""
    return cast("asyncpg.Pool[asyncpg.Record]", pool)


def _gauge_value() -> float:
    """Read the ingest-depth gauge. `prometheus_client` exposes no public
    getter, so this reaches for the internal value in one place rather than at
    every call site."""
    value = cast(
        "float",
        INGEST_QUEUE_DEPTH._value.get(),  # pyright: ignore[reportPrivateUsage]
    )
    return value


def _click(link_id: int = 1) -> ClickEvent:
    return ClickEvent(link_id=link_id, referer=None, user_agent="test-agent", ip_hash=None)


# --------------------------------------------------------------------------- #
# batching
# --------------------------------------------------------------------------- #


async def test_a_batch_is_exactly_one_statement() -> None:
    """The SPEC's "verifiable by counting statements". 250 clicks, one INSERT."""
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder))
    batch = [_click(i) for i in range(250)]

    written = await ingestor.flush(batch)

    assert written == 250
    assert len(recorder.statements) == 1
    query, args = recorder.statements[0]
    assert query.startswith("INSERT INTO click_events")
    # Four bound parameters per row, and every value travels as a parameter -
    # so no user-controlled string is ever concatenated into the SQL.
    assert len(args) == 250 * 4
    assert query.count("(") == 251  # the column list, plus one tuple per row


async def test_flush_drains_the_batch() -> None:
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder))
    batch = [_click(), _click()]

    await ingestor.flush(batch)

    assert batch == []


async def test_empty_flush_touches_nothing() -> None:
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder))
    assert await ingestor.flush([]) == 0
    assert recorder.statements == []


async def test_a_failing_batch_is_dropped_not_stuck() -> None:
    """Clicks are analytics. A failed batch is logged and dropped - retrying
    would buy a duplicate-delivery problem in exchange for data nobody
    reconciles - and the ingestor must survive to handle the next one."""
    recorder = RecordingPool(fail=True)
    ingestor = ClickIngestor(_fake_pool(recorder))
    batch = [_click()]

    assert await ingestor.flush(batch) == 0
    assert batch == [], "a failing batch is dropped, not left to retry forever"


# --------------------------------------------------------------------------- #
# the bounded queue and its overflow policy
# --------------------------------------------------------------------------- #


def test_accept_sheds_when_the_queue_is_full() -> None:
    """The declared overflow policy: drop. `accept` reports it rather than
    blocking, because a redirect must never wait on analytics."""
    ingestor = ClickIngestor(_fake_pool(RecordingPool()), capacity=3)

    accepted = [ingestor.sink.accept(_click()) for _ in range(5)]

    assert accepted == [True, True, True, False, False]
    assert ingestor.queue_depth == 3


def test_accept_never_blocks() -> None:
    """`accept` is deliberately not a coroutine - there is no way to `await` it,
    so a handler cannot accidentally block a redirect on ingestion. This pins
    that as a contract rather than an accident of the current implementation."""
    ingestor = ClickIngestor(_fake_pool(RecordingPool()))
    assert not inspect.iscoroutinefunction(ingestor.sink.accept)


def test_queue_depth_gauge_tracks_accepts() -> None:
    ingestor = ClickIngestor(_fake_pool(RecordingPool()), capacity=10)
    before = _gauge_value()

    ingestor.sink.accept(_click())
    ingestor.sink.accept(_click())

    assert _gauge_value() == before + 2


def test_a_closed_sink_refuses_clicks() -> None:
    ingestor = ClickIngestor(_fake_pool(RecordingPool()))
    ingestor.sink.close()
    assert ingestor.sink.accept(_click()) is False


# --------------------------------------------------------------------------- #
# the run loop
# --------------------------------------------------------------------------- #


async def test_run_flushes_on_a_full_batch() -> None:
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder), max_batch=5, flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())

    for _ in range(5):
        ingestor.sink.accept(_click())
    await asyncio.sleep(0.05)

    # The size trigger fired; the long interval proves it was not the timer.
    assert len(recorder.statements) == 1

    await ingestor.stop()
    await task


async def test_run_flushes_on_the_interval() -> None:
    """The time half of "N rows or every T ms" - a trickle must not sit forever."""
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder), max_batch=1_000, flush_interval=0.05)
    task = asyncio.create_task(ingestor.run())

    ingestor.sink.accept(_click())
    await asyncio.sleep(0.2)

    assert len(recorder.statements) == 1

    await ingestor.stop()
    await task


async def test_stop_flushes_what_is_buffered() -> None:
    """Graceful shutdown: a clean stop loses nothing."""
    recorder = RecordingPool()
    ingestor = ClickIngestor(_fake_pool(recorder), max_batch=1_000, flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())

    for _ in range(3):
        ingestor.sink.accept(_click())
    await asyncio.sleep(0)

    await ingestor.stop()
    await task

    assert len(recorder.statements) == 1
    assert len(recorder.statements[0][1]) == 3 * 4


# --------------------------------------------------------------------------- #
# against a real database
# --------------------------------------------------------------------------- #


@pytest.fixture
async def seeded_link(pg_pool: asyncpg.Pool[asyncpg.Record]) -> int:
    """A links row to hang clicks off - `click_events.link_id` has an FK."""
    link_id = abs(hash(unique_slug("ing"))) % (2**62)
    await pg_pool.execute(
        "INSERT INTO links (id, slug, long_url) VALUES ($1, $2, $3)",
        link_id,
        unique_slug("ingest"),
        "https://example.com",
    )
    return link_id


async def test_batch_rows_actually_land(
    pg_pool: asyncpg.Pool[asyncpg.Record], seeded_link: int
) -> None:
    ingestor = ClickIngestor(pg_pool)
    await ingestor.flush([_click(seeded_link) for _ in range(7)])

    count = await pg_pool.fetchval(
        "SELECT COUNT(*) FROM click_events WHERE link_id = $1", seeded_link
    )
    assert count == 7


async def test_a_bad_batch_commits_nothing(
    pg_pool: asyncpg.Pool[asyncpg.Record], seeded_link: int
) -> None:
    """One statement means one atomic unit: a batch containing an invalid row
    (no such link) writes none of its rows, rather than half of them."""
    ingestor = ClickIngestor(pg_pool)
    await ingestor.flush([_click(seeded_link), _click(2**62 - 1)])

    count = await pg_pool.fetchval(
        "SELECT COUNT(*) FROM click_events WHERE link_id = $1", seeded_link
    )
    assert count == 0


async def test_run_loop_drains_to_postgres(
    pg_pool: asyncpg.Pool[asyncpg.Record], seeded_link: int
) -> None:
    ingestor = ClickIngestor(pg_pool, flush_interval=60.0)
    task = asyncio.create_task(ingestor.run())

    for _ in range(4):
        ingestor.sink.accept(_click(seeded_link))
    await asyncio.sleep(0)

    await ingestor.stop()
    await task

    count = await pg_pool.fetchval(
        "SELECT COUNT(*) FROM click_events WHERE link_id = $1", seeded_link
    )
    assert count == 4
