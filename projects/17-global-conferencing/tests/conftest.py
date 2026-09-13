"""Fixtures for the scaffold suite.

Two decisions worth stating:

**Every SFU binds ephemeral ports** (`MEDIA_PORT=0`, `CASCADE_PORT=0`). Both
planes are real UDP sockets opened in the lifespan, so fixed ports would collide
with a running dev server — and would stop a future test from standing up the
three-region mesh the V1/V2 acceptance tests want, in one process. Ask the bound
endpoint (`state.backbone.local_addr`) for the port that was actually used.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the app
on its own loop in a background thread, out of the test's reach — you could not
send a datagram at the backbone and then await the consequence. `ASGITransport`
runs the app on the *test's* loop; the lifespan is driven explicitly below.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import FastAPI

from global_conferencing.config import Settings
from global_conferencing.main import create_app
from global_conferencing.state import AppState


@pytest.fixture
def settings() -> Settings:
    """A lone SFU (no peers, quorum 1) on ephemeral ports with tiny caps.

    Every field a developer's `.env` could plausibly flip is pinned here, so a
    local `RUN_BACKGROUND=true` never changes what the suite tests.
    """
    return Settings(
        region="eu-west",
        node_id="n1",
        peers="",
        media_port=0,
        cascade_port=0,
        max_rooms=2,
        max_relay_links=2,
        media_inbox=8,
        cascade_inbox=8,
        run_background=False,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """A booted SFU: both sockets bound, backbone pump running, state assembled."""
    application = create_app(settings)
    # `ASGITransport` does not speak the lifespan half of ASGI, so drive it by hand.
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sfu.test") as http:
        yield http


@pytest.fixture
def state(app: FastAPI) -> AppState:
    app_state = app.state.app_state
    assert isinstance(app_state, AppState)
    return app_state
