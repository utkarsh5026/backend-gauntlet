"""Shared fixtures.

Note the HTTP client: `httpx.AsyncClient` over `ASGITransport` rather than
Starlette's `TestClient`. It exercises the app through the same ASGI interface
uvicorn uses, keeps the tests genuinely async (so an `await` bug shows up as
one), and avoids `TestClient`'s sync-portal indirection.

The WebSocket tests are the one exception, and they say so where they live:
httpx has no WebSocket client at all, so `TestClient.websocket_connect` is the
only way to drive `/ws` in-process.

Note also what the fixture does *not* do: it never reaches for Redis or
Postgres. The app starts single-node and DB-free by design (see `main.py`),
which is what lets the whole surface be tested with no containers running.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from pydantic import SecretStr

from realtime_pubsub.backpressure import Mailbox, OverflowPolicy
from realtime_pubsub.config import Settings
from realtime_pubsub.main import create_app
from realtime_pubsub.protocol import BroadcastMessage, ServerMessage

TEST_TOKEN = "test-token"


@pytest.fixture
def settings() -> Settings:
    """A standalone config: single node, no roster DB, a known auth token.

    The sweep interval is deliberately long so the background task never fires
    mid-test — presence expiry is tested directly against `sweep()` instead,
    where it is deterministic.
    """
    return Settings(
        port=8080,
        cluster=False,
        database_url="",
        ws_auth_token=SecretStr(TEST_TOKEN),
        outbox_capacity=8,
        overflow_policy=OverflowPolicy.DROP_NEWEST,
        presence_ttl_secs=30.0,
        presence_sweep_interval_secs=3600.0,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted app.

    Entering `lifespan_context` runs the real startup path — state assembled,
    background tasks spawned, shutdown drained afterwards — so a test can never
    pass against wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://pubsub") as http:
            yield http


def mailbox(capacity: int, policy: OverflowPolicy) -> Mailbox:
    """Terser constructor for the many mailboxes the V1/V2 tests build."""
    return Mailbox(capacity, policy)


def broadcast(topic: str, payload: str) -> BroadcastMessage:
    """A `message` frame, the thing that actually gets fanned out."""
    return BroadcastMessage(topic=topic, payload=payload)


def payloads(box: Mailbox) -> list[str]:
    """Payload of every buffered broadcast, in order.

    Narrowing to `BroadcastMessage` is not busywork for the type checker: a
    mailbox legitimately carries `presence` and `error` frames too, and a test
    that assumed every frame was a broadcast would start failing the day
    presence started being announced.
    """
    return [str(m.payload) for m in drain(box) if isinstance(m, BroadcastMessage)]


def payload_of(message: ServerMessage | None) -> str | None:
    """The payload of one broadcast frame, or `None` for anything else."""
    return str(message.payload) if isinstance(message, BroadcastMessage) else None


def drain(box: Mailbox) -> list[ServerMessage]:
    """Everything currently buffered, without awaiting."""
    out: list[ServerMessage] = []
    while (message := box.try_recv()) is not None:
        out.append(message)
    return out
