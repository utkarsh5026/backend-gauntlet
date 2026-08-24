"""Shared fixtures.

The acceptance tests for V1-V5 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness.

Note the client: `httpx.AsyncClient` over `ASGITransport` rather than Starlette's
`TestClient`. It exercises the app through the same ASGI interface uvicorn uses,
keeps the tests genuinely async (so an `await` bug in your code surfaces as one),
and avoids `TestClient`'s sync-portal indirection — which would run your
coroutines on a different loop than production does.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from full_text_search.analyzer import Analyzer
from full_text_search.config import Settings
from full_text_search.main import create_app
from full_text_search.shard import ShardedIndex


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A small engine rooted in a per-test temp directory.

    `tmp_path` matters more here than in most projects: the index *is* the
    filesystem, so without it every test would share one `./data` and segments
    from a previous run would show up as search hits in the next.

    Two shards, not one — a single-shard engine cannot catch a routing bug or a
    fan-out that forgot to merge, which are half of V5.
    """
    return Settings(
        index_dir=tmp_path / "index",
        shard_count=2,
        refresh_interval_ms=0,
        query_cache_cap=0,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted engine.

    Entering `lifespan_context` runs the real startup path — the shard
    directories are created and the background tasks started, then torn down
    after — so a test can never pass against wiring that would fail in
    production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://search") as http_client:
            yield http_client


@pytest.fixture
def engine(settings: Settings) -> ShardedIndex:
    """The coordinator with no HTTP around it.

    Most of what the SPEC grades lives below the transport, and reaching it
    directly keeps those tests honest — a routing or ranking assertion should not
    have to go through a JSON round-trip to be made.
    """
    return ShardedIndex(settings.engine, Analyzer())
