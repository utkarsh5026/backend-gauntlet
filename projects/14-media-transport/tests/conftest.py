"""Fixtures for the scaffold suite.

Two decisions worth stating, because both are the kind of thing that is easy to
get wrong once and then live with:

**Every transport binds an ephemeral UDP port** (`RTP_PORT=0`). The media plane
is a real socket opened in the lifespan, so a fixed port would make the suite
fail whenever a dev server is running — and would stop two tests from ever
holding a transport at the same time. Ask `MediaSocket.local_addr` for the port
that was actually bound.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the
app on its own event loop in a background thread, which for this project means
the media socket and the session task live on a loop the test cannot reach — you
could not send a datagram at the transport and then await the consequence.
`ASGITransport` runs the app on the *test's* loop, in-process, with no socket in
front of it, and the lifespan is driven explicitly below.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import FastAPI

from media_transport.config import Role, Settings
from media_transport.main import create_app
from media_transport.state import AppState


@pytest.fixture
def settings() -> Settings:
    """Config for a self-contained receiver on an ephemeral port.

    `rtp_inbox` is deliberately tiny so the bounded-queue criterion is reachable
    in a test without sending a thousand datagrams. `http_port` is left at its
    default and never bound: `ASGITransport` speaks to the app directly, so the
    admin plane in these tests has no socket at all.
    """
    return Settings(role=Role.RECEIVER, rtp_port=0, rtp_inbox=8)


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """A booted app: UDP socket bound, session running, state assembled."""
    application = create_app(settings)
    # Drive the lifespan by hand. FastAPI runs it through the ASGI protocol, and
    # `ASGITransport` does not speak the lifespan half of that protocol — so
    # without this the app serves with `app.state.app_state` unset and every
    # handler raises "app state was not initialised".
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transport.test") as http:
        yield http


@pytest.fixture
def state(app: FastAPI) -> AppState:
    """The assembled app state — the media socket and the session task."""
    app_state = app.state.app_state
    assert isinstance(app_state, AppState)
    return app_state
