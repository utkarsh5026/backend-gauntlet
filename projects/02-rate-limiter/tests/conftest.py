"""Shared fixtures.

The acceptance tests for V1-V3 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness.

Two things worth noticing about the gRPC client fixture:

  * It talks over a **real loopback socket**, not an in-process shortcut. gRPC
    has no ASGI-transport equivalent, and that turns out to be a feature here:
    HTTP/2 framing, deadlines and status codes are all genuinely exercised, so a
    test can't pass against wiring that would fail on the wire.
  * The port is **ephemeral** (`add_insecure_port(":0")` returns the one the OS
    picked), so tests never collide with a dev server or with each other.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import grpc
import grpc.aio
import httpx
import pytest
from starlette.applications import Starlette

from rate_limiter.config import Settings
from rate_limiter.main import build_grpc_server, build_state
from rate_limiter.pb import ratelimit_pb2_grpc as rpc
from rate_limiter.routes import create_admin_app
from rate_limiter.state import AppState


@pytest.fixture
def settings() -> Settings:
    """A small, deterministic budget — easier to reason about than the defaults."""
    return Settings(rate_per_sec=10.0, burst=20, fail_open=True)


@pytest.fixture
def state(settings: Settings) -> AppState:
    """The assembled runtime.

    Constructing `Redis.from_url` does not connect — redis-py dials lazily on
    first command — so this fixture needs no live Redis. Any test that actually
    reaches the backend will need one; that is a V3 concern.
    """
    return build_state(settings)


@pytest.fixture
def admin_app(state: AppState) -> Starlette:
    return create_admin_app(state)


@pytest.fixture
async def admin(admin_app: Starlette) -> AsyncGenerator[httpx.AsyncClient]:
    """The admin HTTP surface, over ASGI — no socket needed."""
    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        yield client


@pytest.fixture
async def grpc_channel(state: AppState) -> AsyncGenerator[grpc.aio.Channel]:
    """A booted server on an ephemeral port, plus a channel pointed at it.

    Yielded as a channel rather than a stub so a test can also reach the health
    and reflection services registered on the same server.
    """
    server, _health = build_grpc_server(state)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield channel
    finally:
        await server.stop(None)
        await state.redis.aclose()


@pytest.fixture
def grpc_stub(grpc_channel: grpc.aio.Channel) -> rpc.RateLimiterAsyncStub:
    return rpc.RateLimiterStub(grpc_channel)
