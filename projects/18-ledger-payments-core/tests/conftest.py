"""Fixtures for the scaffold suite.

The acceptance tests for V1–V4 are yours to write (see each vertical's "Proof"). What
lives here is the harness, split by what a test needs:

* **Anything provable without a service** — the routes, the error mapping, config, the
  wired dispatcher loop and shutdown — runs against `state` / `client` and always runs,
  including in CI, where there is no Postgres or Redis. `state` builds the real app over
  a pool that never connects and a Redis client that never dials; every vertical raises
  its todo before it would touch either.
* **Anything that needs a real dependency** goes through `pg_pool` or `redis`, each of
  which **skips** rather than fails when its service is missing. `make up` starts both.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the app on
its own loop in a background thread, which would put the dispatcher on a loop the test
can't reach — a test couldn't set `state.shutdown` and await the drain.
`ASGITransport` runs the app on the *test's* loop.

**Each `pg_pool` test gets its own database**, cloned from a migrated template. V2's and
V3's criteria are about requests racing on *separate* connections; a
roll-back-each-test transaction would put every racer on one session, where no race can
happen and a broken transfer passes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ledger_payments_core.config import Settings
from ledger_payments_core.db import MIGRATIONS_DIR, create_pool, run_migrations
from ledger_payments_core.main import build_state, create_app, create_redis
from ledger_payments_core.state import AppState

TEMPLATE_DB = "ledger_payments_core_test_template"
"""Migrated once per session, then cloned per test."""

TEST_REDIS_DB = 15
"""The `redis` fixture flushes this logical database, so it stays clear of the 0 a
running dev server uses — unless `REDIS_URL` pins a database of its own, which wins."""

_template_ready = False


@pytest.fixture
def settings() -> Settings:
    """Config for a test instance — explicit where a developer's `.env` could otherwise
    change the outcome: the dispatcher off, and a short dispatch interval so a loop
    reaches its first round in milliseconds."""
    return Settings(run_dispatcher=False, webhook_dispatch_interval_ms=10)


@pytest.fixture
async def state(settings: Settings) -> AsyncGenerator[AppState]:
    """The assembled ledger, over a pool that never connects and a lazy Redis client.

    Async so a loop is running when the pool is constructed: asyncpg binds a `Pool` to
    the current loop in `__init__`.
    """
    pool: asyncpg.Pool[asyncpg.Record] = asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=2
    )
    client = create_redis(settings)
    try:
        yield build_state(settings, pool=pool, redis=client)
    finally:
        await client.aclose()


@pytest.fixture
async def app(state: AppState) -> AsyncGenerator[FastAPI]:
    """A booted app over `state`."""
    application = create_app(state=state)
    # Drive the lifespan by hand: `ASGITransport` does not speak the lifespan half of
    # ASGI, so without this `app.state.app_state` is never set.
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ledger.test") as http:
        yield http


# --------------------------------------------------------------------------- #
# Real dependencies — each skips when it is not available
# --------------------------------------------------------------------------- #


def _with_database(dsn: str, name: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{name}"))


async def _ensure_template(admin_dsn: str, dsn: str) -> None:
    """Create and migrate the template database, once per test session."""
    global _template_ready
    if _template_ready:
        return
    admin: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin_dsn)
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEMPLATE_DB):
            await admin.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
    finally:
        await admin.close()
    # Migrate, then disconnect: `CREATE DATABASE … TEMPLATE` refuses to run while
    # anything else is connected to the template.
    pool = await create_pool(_with_database(dsn, TEMPLATE_DB), min_size=1, max_size=2)
    try:
        await run_migrations(pool, MIGRATIONS_DIR)
    finally:
        await pool.close()
    _template_ready = True


@pytest.fixture
async def pg_pool(settings: Settings) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record]]:
    """A pool onto a freshly cloned, migrated database — or a skip.

    Sized for races: V2's storm needs more connections than racers-minus-one, or the
    pool itself serializes them and the test proves nothing.
    """
    admin_dsn = _with_database(settings.database_url, "postgres")
    name = f"ledger_payments_core_test_{uuid4().hex[:12]}"
    try:
        await _ensure_template(admin_dsn, settings.database_url)
        admin: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin_dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable ({exc}) — run `make up`")

    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
    pool = await create_pool(_with_database(settings.database_url, name), min_size=1, max_size=20)
    try:
        yield pool
    finally:
        await pool.close()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def redis(settings: Settings) -> AsyncGenerator[Redis]:
    """A real, empty Redis database — or a skip."""
    # redis-py types `from_url`'s **kwargs loosely; the returned client is typed.
    client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url, db=TEST_REDIS_DB, decode_responses=True
    )
    try:
        await client.flushdb()  # pyright: ignore[reportUnknownMemberType]  # untyped **kwargs
    except (OSError, RedisError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable ({exc}) — run `make up`")
    try:
        yield client
    finally:
        await client.flushdb()  # pyright: ignore[reportUnknownMemberType]  # untyped **kwargs
        await client.aclose()
