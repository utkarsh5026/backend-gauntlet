"""Shared fixtures.

The acceptance tests for V1-V5 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness: a node with an isolated data directory, and a
client that speaks to it over ASGI.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from dynamodb_core.config import Settings
from dynamodb_core.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A node with its own data dir, so tests never share on-disk state."""
    return Settings(
        port=8000,
        data_dir=tmp_path / "data",
        # Small enough that a capacity test can exhaust it deliberately.
        default_read_capacity=100,
        default_write_capacity=100,
        partition_write_capacity=10,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted node.

    Entering `lifespan_context` runs the real startup path, so a test can never
    pass against wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ddb") as http_client:
            yield http_client
