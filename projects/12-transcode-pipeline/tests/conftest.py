"""Fixtures for the scaffold suite.

The acceptance tests for V1–V4 are yours to write (see each vertical's "Proof").
What lives here is the harness, split by what a test needs:

* **Anything provable without a service** — the routes, the error mapping, config,
  the wired worker/scheduler loops, the ffmpeg plumbing against fake tools — runs
  against `state` / `client` and always runs, including in CI, where there is no
  Postgres. `state` builds the real app over a pool that never connects: asyncpg's
  `create_pool()` is unconnected until awaited, and every store method raises its
  todo before it would touch it.
* **Anything that needs a real dependency** goes through `pg_pool` or
  `ffmpeg_tools`, each of which **skips** rather than fails when its dependency is
  missing. `make up` starts Postgres; ffmpeg has to be on `PATH`.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the app on
its own loop in a background thread, which would put the worker tasks on a loop the
test cannot reach — so a test could not set `state.shutdown` and await the drain.
`ASGITransport` runs the app on the *test's* loop.

**Each `pg_pool` test gets its own database**, cloned from a migrated template.
V3's claim criterion is about workers racing on *separate* connections; a
roll-back-each-test transaction would put both on one session and let a claim with a
real race pass.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from transcode_pipeline.config import Settings
from transcode_pipeline.db import MIGRATIONS_DIR, create_pool, run_migrations
from transcode_pipeline.main import build_state, create_app
from transcode_pipeline.state import AppState

TEMPLATE_DB = "transcode_pipeline_test_template"
"""Migrated once per session, then cloned per test."""

_template_ready = False


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Config for a test instance.

    Explicit where a developer's `.env` could otherwise change the outcome: workers
    off, `WORK_DIR` in a per-test temp dir, and intervals short enough that a loop
    reaches its first store call in milliseconds rather than half a second.
    """
    return Settings(
        run_workers=False,
        work_dir=tmp_path / "work",
        scheduler_interval_ms=10,
        poll_interval_ms=10,
    )


@pytest.fixture
async def state(settings: Settings) -> AsyncGenerator[AppState]:
    """The assembled pipeline, over a pool that never connects.

    Async so a loop is running when the pool is constructed: asyncpg binds a `Pool`
    to the current loop in `__init__`.
    """
    pool: asyncpg.Pool[asyncpg.Record] = asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=2
    )
    yield build_state(settings, pool=pool)


@pytest.fixture
async def app(state: AppState) -> AsyncGenerator[FastAPI]:
    """A booted app over `state`."""
    application = create_app(state=state)
    # Drive the lifespan by hand: `ASGITransport` does not speak the lifespan half
    # of ASGI, so without this `app.state.app_state` is never set.
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transcode.test") as http:
        yield http


# --------------------------------------------------------------------------- #
# Real dependencies — each skips when it is not available
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FfmpegTools:
    ffmpeg: str
    ffprobe: str


@pytest.fixture
def ffmpeg_tools(settings: Settings) -> FfmpegTools:
    """Absolute paths to ffmpeg + ffprobe — or a skip.

    CI has no ffmpeg; the V3/V4 Proofs (determinism, no seam) need a real one.
    """
    ffmpeg = shutil.which(settings.ffmpeg_bin)
    ffprobe = shutil.which(settings.ffprobe_bin)
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return FfmpegTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


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
    name = f"transcode_pipeline_test_{uuid4().hex[:12]}"
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
