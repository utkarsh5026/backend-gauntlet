"""Shared fixtures.

The acceptance tests for V1-V4 are yours to write. What lives here is only the
harness.

Note the client: `httpx.AsyncClient` over `ASGITransport` rather than Starlette's
`TestClient`. It exercises the app through the same ASGI interface uvicorn uses,
keeps the tests genuinely async (so an `await` bug in your code shows up as one),
and avoids `TestClient`'s sync-portal indirection.

Note also what the fixture does *not* do: it never reaches for NATS or
ClickHouse. The app is designed to start degraded when they are absent (see
`main.py`), which is what lets the whole HTTP surface be tested with no
containers running. Tests that need a real broker belong in their own file,
skipped when it isn't there.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest

from metrics_pipeline.config import Settings
from metrics_pipeline.main import create_app


@pytest.fixture
def settings() -> Settings:
    """A standalone config: no consumer, an unreachable broker and store.

    The unroutable ports are deliberate — the connection attempt fails fast and
    the app comes up degraded, which is the state under test.
    """
    return Settings(
        port=8080,
        nats_url="nats://127.0.0.1:1",
        broker_connect_timeout=0.2,
        clickhouse_url="http://127.0.0.1:1",
        run_consumer=False,
        sse_capacity=8,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted app.

    Entering `lifespan_context` runs the real startup path — connections
    attempted, state assembled, shutdown drained afterwards — so a test can never
    pass against wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://pipeline") as http:
            yield http
