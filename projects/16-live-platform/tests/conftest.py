"""Fixtures for the scaffold suite.

The acceptance tests for V1–V4 are yours to write (see each vertical's "Proof").
What lives here is the harness, split by what a test needs:

* **Anything provable without a service** — the routes, the error mapping,
  config, the wired parts of each plane — runs against `state` / `client` and
  always runs, including in CI, where there is no Postgres, Redis or NATS.
  `state` builds the real platform over handles that never connect: asyncpg's
  `create_pool()` is unconnected until awaited, `Redis.from_url` dials lazily,
  and an unconnected NATS client still hands out a JetStream context. A test
  that reaches real I/O fails loudly rather than hanging.
* **Anything that needs a real dependency** goes through `pg_pool`, `redis` or
  `nats_client`, each of which **skips** rather than fails when its service is
  not up. `make up` starts all three.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the
app on its own loop in a background thread, which would put the edge's in-flight
futures and the chat outboxes on a loop the test cannot reach — so you could not
hold a blocking reload open in one coroutine and produce the part that releases
it from another. `ASGITransport` runs the app on the *test's* loop. (It does not
speak WebSocket; a chat test drives `ChatHub` directly, or boots uvicorn.)

**Each `pg_pool` test gets its own database**, cloned from a migrated template.
V1's idempotency criterion is about two webhook deliveries racing on *separate*
connections; a roll-back-each-test transaction would put both on one session and
let code with a real race pass.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from live_platform.config import Settings
from live_platform.db import MIGRATIONS_DIR, create_pool, run_migrations
from live_platform.main import build_state, create_app
from live_platform.state import AppState

TEMPLATE_DB = "live_platform_test_template"
"""Migrated once per session, then cloned per test."""

_template_ready = False


def _origin_unreachable(request: httpx.Request) -> httpx.Response:
    """The packager origin, as far as the scaffold suite is concerned: absent.

    An `httpx.MockTransport` handler, so no test ever opens a real socket to an
    origin. It is also the seam for V3's proofs — swap in a handler that counts
    calls and sleeps, fire 1,000 concurrent requests at one cold part, and
    assert the count.
    """
    raise httpx.ConnectError("no packager origin in the test suite", request=request)


@pytest.fixture
def settings() -> Settings:
    """Config for a test instance.

    Explicit where a developer's `.env` could otherwise change the outcome:
    background loops off, and a tiny outbox so an overflow is reachable in a test
    without publishing thousands of messages.
    """
    return Settings(run_background=False, outbox_capacity=8)


@pytest.fixture
async def state(settings: Settings) -> AsyncGenerator[AppState]:
    """The assembled platform, over handles that never connect.

    Async so a loop is running when the pool is constructed: asyncpg binds a
    `Pool` to the current loop in `__init__`.
    """
    pool: asyncpg.Pool[asyncpg.Record] = asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=2
    )
    # redis-py types `from_url`'s **kwargs loosely; the one argument is a `str`.
    redis = Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
    nats_client = NatsClient()
    http = httpx.AsyncClient(transport=httpx.MockTransport(_origin_unreachable))
    try:
        yield build_state(settings, pool=pool, redis=redis, nats=nats_client, http=http)
    finally:
        await http.aclose()
        await redis.aclose()


@pytest.fixture
async def app(state: AppState) -> AsyncGenerator[FastAPI]:
    """A booted app over `state`."""
    application = create_app(state=state)
    # Drive the lifespan by hand: `ASGITransport` does not speak the lifespan
    # half of ASGI, so without this `app.state.app_state` is never set.
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://live.test") as http:
        yield http


# --------------------------------------------------------------------------- #
# Real dependencies — each skips when its service is not running
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
    """A pool onto a freshly cloned, migrated database — or a skip."""
    admin_dsn = _with_database(settings.database_url, "postgres")
    name = f"live_platform_test_{uuid4().hex[:12]}"
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
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def redis(settings: Settings) -> AsyncGenerator[Redis]:
    """A connected Redis client — or a skip. Two of these make a two-pod V4 test."""
    client = Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except (RedisConnectionError, OSError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable ({exc}) — run `make up`")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def nats_client(settings: Settings) -> AsyncGenerator[NatsClient]:
    """A connected NATS client — or a skip."""
    client = NatsClient()
    try:
        await client.connect(settings.nats_url, connect_timeout=1, allow_reconnect=False)
    except Exception as exc:  # noqa: BLE001
        # Any failure to reach the broker is a skip, not a failure; which
        # exception type you get depends on how far the handshake got.
        pytest.skip(f"NATS not reachable ({exc}) — run `make up`")
    try:
        yield client
    finally:
        await client.close()
