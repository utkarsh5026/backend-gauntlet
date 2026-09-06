"""Fixtures for the scaffold suite.

Two decisions worth stating, because both are the kind of thing that is easy to
get wrong once and then live with:

**Every SFU binds an ephemeral media port** (`MEDIA_PORT=0`). The media plane is
a real UDP socket opened in the lifespan, so a fixed port would make the suite
fail whenever a dev server is running — and would stop two tests from ever
holding an SFU at the same time. Ask `MediaSocket.local_addr` for the port that
was actually bound.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the
app on its own event loop in a background thread, which for this project means
the media socket and the pump live on a loop the test cannot reach — you could
not send a datagram at the SFU and then await the consequence. `ASGITransport`
runs the app on the *test's* loop, in-process, no sockets in front of it, and
the lifespan is driven explicitly by `LifespanManager` below.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import FastAPI

from webrtc_sfu.config import Settings
from webrtc_sfu.main import create_app
from webrtc_sfu.state import AppState


@pytest.fixture
def settings() -> Settings:
    """Config for a self-contained SFU: ephemeral media port, small caps.

    The caps are deliberately tiny so the "room full" and "too many rooms"
    criteria are reachable in a test without creating sixty-four of anything.
    """
    return Settings(
        media_port=0,
        max_rooms=2,
        max_peers_per_room=3,
        media_inbox=8,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """A booted app: media socket bound, pump running, state assembled."""
    application = create_app(settings)
    # Drive the lifespan by hand. FastAPI runs it through the ASGI protocol,
    # and `ASGITransport` does not speak the lifespan half of that protocol --
    # so without this the app serves with `app.state.app_state` unset and every
    # handler raises "app state was not initialised".
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sfu.test") as http:
        yield http


@pytest.fixture
def state(app: FastAPI) -> AppState:
    """The assembled app state — the SFU core, the media socket, the pump task."""
    app_state = app.state.app_state
    assert isinstance(app_state, AppState)
    return app_state
