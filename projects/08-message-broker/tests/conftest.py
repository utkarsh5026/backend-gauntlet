"""Shared fixtures.

The acceptance tests for V1-V4 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness.

Note the client: `httpx.AsyncClient` over `ASGITransport` rather than Starlette's
`TestClient`. It exercises the app through the same ASGI interface uvicorn uses,
keeps the tests genuinely async (so an `await` bug in your code shows up as one),
and avoids `TestClient`'s sync-portal indirection.

Note also `tmp_path`: every test gets its own `data_dir`. The broker's entire
state is the filesystem, so a shared directory would let one test's segments
become another's "recovered" log — and the restart tests V1 asks for depend on
controlling exactly what is on disk.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from message_broker.config import Settings
from message_broker.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A broker over a throwaway data dir.

    `segment_bytes` is tiny on purpose: V1 grades that the log *rolls*, and at
    the 64 MiB default a test would have to write 64 MiB to see it happen once.
    """
    return Settings(
        port=9092,
        data_dir=tmp_path / "data",
        segment_bytes=4096,
        index_interval_bytes=256,
        default_partitions=3,
        max_record_bytes=64 * 1024,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted broker.

    Entering `lifespan_context` runs the real startup path — the data dir is
    opened and existing topics reloaded, then flushed on the way out — so a test
    can never pass against wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://broker") as http_client:
            yield http_client
