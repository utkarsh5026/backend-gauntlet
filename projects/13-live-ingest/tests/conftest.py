"""Fixtures for the scaffold suite.

Two decisions worth stating, because both are easy to get wrong once and then
live with:

**The RTMP server binds an ephemeral port** (`RTMP_PORT=0`). It is a real TCP
listener opened in the lifespan, so a fixed port would fail whenever a dev server
is running. Ask `RtmpIngest.port` for the port that was actually bound.

**httpx `ASGITransport`, not Starlette's `TestClient`.** `TestClient` runs the
app on its own event loop in a background thread, which would put the live
window and every held blocking reload on a loop the test cannot reach — you
could not push a part and then await the request it unparks. `ASGITransport`
runs the app on the *test's* loop, in-process, and the lifespan is driven
explicitly below.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import FastAPI

from live_ingest.config import Settings
from live_ingest.main import create_app
from live_ingest.state import AppState


@pytest.fixture
def settings() -> Settings:
    """A self-contained server: ephemeral RTMP port, a one-key allow-list, and a
    small window so eviction is reachable in a test without pushing hundreds of
    segments. `http_port` is never bound — `ASGITransport` has no socket."""
    return Settings(
        rtmp_port=0,
        stream_keys="testkey",
        target_part_secs=0.3,
        target_segment_secs=2.0,
        live_window_segments=3,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """A booted app: RTMP listener bound, registry and state assembled."""
    application = create_app(settings)
    # `ASGITransport` does not speak the lifespan half of ASGI, so drive it by
    # hand — without this every handler raises "app state was not initialised".
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client wired straight into the app, no socket in between."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingest.test") as http:
        yield http


@pytest.fixture
def state(app: FastAPI) -> AppState:
    """The assembled state — registry, ingest server, settings."""
    app_state = app.state.app_state
    assert isinstance(app_state, AppState)
    return app_state
