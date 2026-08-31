"""Shared fixtures.

The suite is split by what it needs. Anything that can be proven without a
server - the id generator, URL validation, the rate limiter, the cache codec,
the batching SQL - is a plain unit test and always runs, including in CI where
there is no Postgres and no Redis. The fixtures below cover the rest, and each
one **skips** rather than fails when its backing service is not up, so
`uv run pytest` is honest on a laptop with nothing started.

The HTTP client is `httpx.AsyncClient` over `ASGITransport` rather than
Starlette's `TestClient`: it drives the app through the same ASGI interface
uvicorn uses, keeps the tests genuinely async (so an `await` bug in the code
shows up as one), and skips `TestClient`'s sync-portal indirection.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from url_shortener.cache import Cache, create_redis
from url_shortener.config import Settings
from url_shortener.db import create_pool, run_migrations
from url_shortener.id_gen import IdGenerator
from url_shortener.ingest import ClickIngestor
from url_shortener.main import create_app
from url_shortener.ratelimit import RateLimiter
from url_shortener.state import AppState

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

API_KEY = "test-secret-key"


def unique_slug(prefix: str) -> str:
    """A slug no other test (or run) will collide with.

    The integration tests share one real database on purpose - it is the same
    database the service uses - so isolation comes from unique names rather than
    from truncating tables out from under a parallel test.
    """
    return f"{prefix}-{uuid4().hex[:12]}"


@pytest.fixture
def settings() -> Settings:
    """Config for a test instance: one known API key, everything else from
    `.env`/defaults so it points at the compose services."""
    return Settings(api_keys={API_KEY})


@pytest.fixture
async def pg_pool(settings: Settings) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record]]:
    """A migrated Postgres pool, or a skip."""
    try:
        pool = await create_pool(settings.database_url, min_size=1, max_size=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable ({exc}) - run `make up`")

    try:
        await run_migrations(pool, MIGRATIONS_DIR)
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncGenerator[Redis]:
    """A live Redis client, or a skip."""
    client = create_redis(settings.redis_url)
    try:
        # redis-py types these commands with untyped `**kwargs`, so pyright reports
        # the calls as partially unknown even though the return types are exact.
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except (RedisError, OSError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable ({exc}) - run `make up`")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cache(redis_client: Redis) -> AsyncGenerator[Cache]:
    """A cache scoped to its own key prefix, wiped on the way out.

    The prefix is what lets these tests run against the same Redis a dev server
    is using without either one seeing the other's keys.
    """
    prefix = f"test:{uuid4().hex[:12]}:"
    scoped = Cache(redis_client, key_prefix=prefix)
    try:
        yield scoped
    finally:
        # redis-py types `scan_iter` as an untyped async iterator, so the element
        # type is unknown to pyright; the cast states what it actually yields.
        scan = cast(
            "AsyncIterator[str]",
            redis_client.scan_iter(match=f"{prefix}*"),  # pyright: ignore[reportUnknownMemberType]
        )
        keys: list[str] = [key async for key in scan]
        if keys:
            await redis_client.delete(*keys)


@pytest.fixture
async def client(
    settings: Settings,
    pg_pool: asyncpg.Pool[asyncpg.Record],
    redis_client: Redis,
) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted app.

    Entering `lifespan_context` runs the real startup path - the pool is opened,
    the ingestor task is spawned, and both are torn down afterwards - so a test
    can never pass against wiring that would fail in production. Depending on
    `pg_pool` and `redis_client` is what makes it skip (rather than hang) when
    the services are down, and guarantees migrations have run.
    """
    _ = (pg_pool, redis_client)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://shortener", follow_redirects=False
        ) as http_client:
            yield http_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def app_state(
    settings: Settings,
    pg_pool: asyncpg.Pool[asyncpg.Record],
    cache: Cache,
) -> AppState:
    """The assembled runtime, without an HTTP server in front of it.

    Lets the resolution tests drive the read path directly, which is where the
    cache-aside policy lives - going through HTTP would only add a layer between
    the assertion and the behaviour being asserted.
    """
    return AppState(
        settings=settings,
        pool=pg_pool,
        cache=cache,
        ids=IdGenerator(settings.node_id),
        clicks=ClickIngestor(pg_pool).sink,
        limiter=RateLimiter(),
    )


@pytest.fixture
def settings_node_id(settings: Settings) -> int:
    return settings.node_id
