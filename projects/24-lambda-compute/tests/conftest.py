"""Shared fixtures.

The acceptance tests for V1-V6 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness: a node with an isolated sandbox root, and
clients that speak to **both** of its listeners over ASGI.

Both apps share one `AppState`, exactly as they do in production — so a test can
submit an invocation on the control-plane client and answer it on the runtime
client, which is the loop V1 exists to close.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from lambda_compute.config import Settings
from lambda_compute.main import create_stack


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A node with its own sandbox root, so tests never share on-disk state."""
    return Settings(
        port=9001,
        runtime_api_port=9002,
        sandbox_root=tmp_path / "run",
        # Small enough that a concurrency test can exhaust it deliberately.
        account_concurrency_limit=10,
        burst_concurrency=5,
        scale_up_rate_per_second=10,
        max_environments=8,
        # Short enough that a reaping test doesn't take five minutes.
        environment_idle_ttl_seconds=1.0,
        async_queue_size=16,
    )


@pytest.fixture
async def stack(settings: Settings) -> AsyncGenerator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """A booted node: `(control, runtime)` clients over one shared state.

    Entering `lifespan_context` runs the real startup path, so a test can never
    pass against wiring that would fail in production.
    """
    app, runtime_app = create_stack(settings)
    async with app.router.lifespan_context(app):
        control_transport = httpx.ASGITransport(app=app)
        runtime_transport = httpx.ASGITransport(app=runtime_app)
        async with (
            httpx.AsyncClient(transport=control_transport, base_url="http://lambda") as control,
            httpx.AsyncClient(transport=runtime_transport, base_url="http://runtime") as runtime,
        ):
            yield control, runtime


@pytest.fixture
async def client(
    stack: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> httpx.AsyncClient:
    """The control plane alone — what most tests want."""
    return stack[0]


@pytest.fixture
async def runtime_client(
    stack: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> httpx.AsyncClient:
    """The Runtime API alone — what a fake runtime polls."""
    return stack[1]
