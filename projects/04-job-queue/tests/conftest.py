"""Shared fixtures.

The suite is split by what it needs. Anything provable without a database — the
`NewJob` caps, the backoff curve, the bearer check, job dispatch — is a plain unit
test and always runs, including in CI where there is no Postgres. Everything else
goes through :func:`pg_pool`, which **skips** rather than fails when Postgres is
not up, so `uv run pytest` stays honest on a laptop with nothing started.

The database fixture is this project's answer to Rust's `#[sqlx::test]`, and it
matters more here than in most projects: half these tests are *about concurrency*
(N workers racing over one backlog, a reaper sweeping every expired lease, a NOTIFY
that must not leak between queues). Sharing one database across tests would make
those assertions read other tests' rows, and sharing one *connection* — the usual
"wrap each test in a transaction and roll back" trick — cannot work at all, because
`FOR UPDATE SKIP LOCKED` only does anything across separate sessions.

So each test gets a genuinely fresh database, cloned from a migrated template:
`CREATE DATABASE … TEMPLATE …` is a file copy, which is much cheaper than
re-running migrations, and the template is built once per session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg
import httpx
import pytest

from job_queue.config import Settings
from job_queue.db import create_pool, run_migrations
from job_queue.job import Job, JobId, NewJob
from job_queue.main import create_app
from job_queue.queue import Queue

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

TEMPLATE_DB = "job_queue_test_template"
"""Migrated once per session, then cloned per test."""

DEFAULT_MAX_ATTEMPTS = 5
LEASE = 30.0
"""A comfortable lease: long enough that a claimed job stays its holder's for the
duration of a test unless a test deliberately expires it."""

TOKEN = "test-enqueue-token"

_template_ready = False


def _with_database(dsn: str, name: str) -> str:
    """Same DSN, different database name."""
    parts = urlparse(dsn)
    return urlunparse(parts._replace(path=f"/{name}"))


def unique_queue(prefix: str) -> str:
    """A queue name no other test can collide with.

    Even with a per-test database this is worth doing for the `LISTEN`/`NOTIFY`
    tests: the channel name is derived from the queue name, and a unique queue makes
    the "a NOTIFY for B must not wake A" assertion airtight.
    """
    return f"{prefix}_{uuid4().hex[:12]}"


async def _ensure_template(admin_dsn: str, dsn: str) -> None:
    """Create and migrate the template database, once per test session."""
    global _template_ready
    if _template_ready:
        return

    admin: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin_dsn)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEMPLATE_DB)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
    finally:
        await admin.close()

    # Migrate it, then disconnect: CREATE DATABASE … TEMPLATE refuses to run while
    # anything else is connected to the template.
    pool = await create_pool(_with_database(dsn, TEMPLATE_DB), min_size=1, max_size=2)
    try:
        await run_migrations(pool, MIGRATIONS_DIR)
    finally:
        await pool.close()

    _template_ready = True


@pytest.fixture
def settings() -> Settings:
    """Config for a test instance, pointing at the compose Postgres."""
    return Settings(enqueue_token=TOKEN)


@pytest.fixture
async def pg_pool(settings: Settings) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record]]:
    """A pool onto a freshly-cloned, already-migrated database, or a skip."""
    admin_dsn = _with_database(settings.database_url, "postgres")
    name = f"job_queue_test_{uuid4().hex[:12]}"

    try:
        await _ensure_template(admin_dsn, settings.database_url)
        admin: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin_dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable ({exc}) — run `make up`")

    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
    pool = await create_pool(_with_database(settings.database_url, name), min_size=1, max_size=10)
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


@pytest.fixture
def queue(pg_pool: asyncpg.Pool[asyncpg.Record]) -> Queue:
    return Queue(pg_pool, DEFAULT_MAX_ATTEMPTS)


@pytest.fixture
async def client(
    settings: Settings, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted app driven over ASGI.

    `httpx.AsyncClient` over `ASGITransport` rather than Starlette's `TestClient`:
    it drives the app through the same ASGI interface uvicorn uses, keeps the tests
    genuinely async (so an `await` bug shows up as one), and skips `TestClient`'s
    sync-portal indirection.

    The app's own lifespan is bypassed for the pool — `app.state.app_state` is
    swapped for one on the *test* database, since the lifespan would otherwise open
    a pool onto the real one.
    """
    from job_queue.state import AppState

    app = create_app(settings)
    app.state.app_state = AppState(settings=settings, queue=Queue(pg_pool, DEFAULT_MAX_ATTEMPTS))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://job-queue") as http_client:
        yield http_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- #
# Helpers shared by the database-backed suites
# --------------------------------------------------------------------------- #


def new_job(queue_name: str, **overrides: Any) -> NewJob:
    """A valid `send_email`-ish job on `queue_name`, one field at a time overridable."""
    fields: dict[str, Any] = {
        "queue": queue_name,
        "kind": "noop",
        "payload": {"to": "a@b.com"},
    }
    fields.update(overrides)
    return NewJob(**fields)


async def expire_lease(pool: asyncpg.Pool[asyncpg.Record], job_id: JobId) -> None:
    """Fast-forward a claimed job's lease into the past.

    The deterministic stand-in for a crashed / too-slow worker: it is what the
    reaper would see a minute after a worker stopped answering, without the test
    having to wait a minute.
    """
    await pool.execute(
        "UPDATE jobs SET locked_until = now() - interval '1 minute' WHERE id = $1", job_id
    )


async def make_due_now(pool: asyncpg.Pool[asyncpg.Record], job_id: JobId) -> None:
    """Fast-forward a rescheduled job so its backoff has "elapsed" and it is due."""
    await pool.execute("UPDATE jobs SET run_at = now() WHERE id = $1", job_id)


async def get_job(queue: Queue, job_id: JobId) -> Job:
    job = await queue.get(job_id)
    assert job is not None, f"job {job_id} should exist"
    return job


async def row(pool: asyncpg.Pool[asyncpg.Record], job_id: JobId) -> asyncpg.Record:
    """The raw row, for assertions about columns the `Job` model doesn't carry."""
    record: asyncpg.Record | None = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    assert record is not None, f"job {job_id} should exist"
    return record


async def wait_until(predicate: Any, within: float, interval: float = 0.05) -> float | None:
    """Poll `predicate()` until it is true, returning how long it took.

    Returns `None` on timeout. Used instead of a fixed sleep so a passing test is
    fast and a failing one still bounds itself.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < within:
        if await predicate():
            return loop.time() - start
        await asyncio.sleep(interval)
    return None


def assert_aware(value: datetime) -> datetime:
    """Postgres `TIMESTAMPTZ` always comes back tz-aware; assert it before comparing."""
    assert value.tzinfo is not None, "expected a timezone-aware timestamp"
    return value
