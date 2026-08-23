"""Shared fixtures.

The acceptance tests for V1-V4 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness.

Note the client: `httpx.AsyncClient` over `ASGITransport` rather than Starlette's
`TestClient`. It exercises the app through the same ASGI interface uvicorn uses,
keeps the tests genuinely async (so an `await` bug in your code shows up as one),
and avoids `TestClient`'s sync-portal indirection.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncGenerator

import httpx
import pytest

from distributed_cache.config import Settings
from distributed_cache.main import create_app


def free_udp_port() -> int:
    """An ephemeral UDP port, so tests never collide with a dev node or each other."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def settings() -> Settings:
    """A standalone single-node config — no seeds, ephemeral gossip port."""
    return Settings(
        node_id="test-node",
        advertise_host="127.0.0.1",
        port=8070,
        gossip_port=free_udp_port(),
        seeds=[],
        cache_capacity=16,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted node.

    Entering `lifespan_context` runs the real startup path — the gossip socket is
    bound and the background SWIM task started, then torn down after — so a test
    can never pass against wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://node") as http_client:
            yield http_client
