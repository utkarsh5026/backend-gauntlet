"""Shared fixtures.

The acceptance tests for V1–V5 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness, and it is split by what a test
needs:

* Anything provable without a database — the wire marshaling, edge validation,
  the replay fold (V2), the sticky table (V5) — runs against `state`/`grpc_stub`
  and always runs, including in CI where there is no Postgres. Note that `state`
  builds the engine over a pool it never awaits: `asyncpg.create_pool()` returns
  an *unconnected* `Pool`, and nothing in the scaffold gets far enough to use it.
* Anything about the durable log, timers or dispatch (V1, V3, V4) goes through
  `pg_pool`, which **skips** rather than fails when Postgres is not up.

Two things worth noticing about the gRPC client fixture:

* It talks over a **real loopback socket**, not an in-process shortcut. gRPC has
  no ASGI-transport equivalent, and that turns out to be a feature here: HTTP/2
  framing, deadlines and status codes are all genuinely exercised, so a test
  cannot pass against wiring that would fail on the wire.
* The port is **ephemeral** (`add_insecure_port(":0")` returns the one the OS
  picked), so tests never collide with a dev server or with each other.

And one thing about `pg_pool` that this project cannot compromise on: each test
gets its own **database**, cloned from a migrated template, so its "workers" are
genuinely separate sessions. The usual "wrap each test in a transaction and roll
it back" trick cannot work here at all — `FOR UPDATE SKIP LOCKED` does nothing
within one session, so half the SPEC's proofs would pass against code that
double-dispatches every task.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg
import grpc
import grpc.aio
import httpx
import pytest
from starlette.applications import Starlette

from workflow_engine.config import Settings
from workflow_engine.db import MIGRATIONS_DIR, create_pool, run_migrations
from workflow_engine.main import build_grpc_server, build_state
from workflow_engine.pb import workflow_pb2_grpc as rpc
from workflow_engine.routes import create_admin_app
from workflow_engine.state import AppState

TEMPLATE_DB = "workflow_engine_test_template"
"""Migrated once per session, then cloned per test."""

MAX_PAYLOAD = 1024
"""A small payload cap, so the oversize-rejection tests stay cheap."""

_template_ready = False


def _with_database(dsn: str, name: str) -> str:
    """The same DSN, pointed at a different database."""
    parts = urlparse(dsn)
    return urlunparse(parts._replace(path=f"/{name}"))


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

    # Migrate it, then disconnect: `CREATE DATABASE … TEMPLATE` refuses to run
    # while anything else is connected to the template.
    pool = await create_pool(_with_database(dsn, TEMPLATE_DB), min_size=1, max_size=2)
    try:
        await run_migrations(pool, MIGRATIONS_DIR)
    finally:
        await pool.close()

    _template_ready = True


@pytest.fixture
def settings() -> Settings:
    """Config for a test instance: short windows, small payload cap.

    The long-poll and visibility timeouts are deliberately tiny — a test that
    proves "an abandoned task is redelivered" should take milliseconds, not the
    30 seconds the production default would cost.
    """
    return Settings(
        long_poll_timeout_ms=200,
        task_visibility_timeout_ms=500,
        sticky_ttl_ms=200,
        max_payload_bytes=MAX_PAYLOAD,
    )


@pytest.fixture
async def state(settings: Settings) -> AppState:
    """The assembled engine, over a pool that is never connected.

    `asyncpg.create_pool()` is not a coroutine function: it returns a `Pool`
    object and only dials when you await it. Nothing here does, which is exactly
    what makes the wire-level tests below runnable with no database — and a loud
    `InterfaceError` if a test reaches code that expects one.

    Async purely so a loop is running when the `Pool` is constructed: it binds
    itself to the current loop at `__init__`, which is a detail worth knowing the
    day a fixture of yours starts failing with "no current event loop".
    """
    pool: asyncpg.Pool[asyncpg.Record] = asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=2
    )
    return build_state(settings, pool)


@pytest.fixture
def admin_app(state: AppState) -> Starlette:
    return create_admin_app(state)


@pytest.fixture
async def admin(admin_app: Starlette) -> AsyncGenerator[httpx.AsyncClient]:
    """The admin HTTP surface, over ASGI — no socket needed."""
    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        yield client


@pytest.fixture
async def grpc_channel(state: AppState) -> AsyncGenerator[grpc.aio.Channel]:
    """A booted server on an ephemeral port, plus a channel pointed at it.

    Yielded as a channel rather than a stub so a test can also reach the health
    and reflection services registered on the same server.
    """
    server, _health = build_grpc_server(state)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield channel
    finally:
        await server.stop(None)


@pytest.fixture
def grpc_stub(grpc_channel: grpc.aio.Channel) -> rpc.WorkflowServiceAsyncStub:
    return rpc.WorkflowServiceStub(grpc_channel)


@pytest.fixture
async def pg_pool(settings: Settings) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record]]:
    """A pool onto a freshly-cloned, already-migrated database — or a skip.

    This is what the V1/V3/V4 proofs run against. Cloning from a template is a
    file copy, much cheaper than re-running migrations per test.
    """
    admin_dsn = _with_database(settings.database_url, "postgres")
    name = f"workflow_engine_test_{uuid4().hex[:12]}"

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
async def pg_state(settings: Settings, pg_pool: asyncpg.Pool[asyncpg.Record]) -> AppState:
    """A full engine over a real, migrated database."""
    return build_state(settings, pg_pool)
