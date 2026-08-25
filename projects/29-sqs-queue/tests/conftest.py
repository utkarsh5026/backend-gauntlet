"""Shared fixtures.

The acceptance tests for V1-V6 are yours to write (see the SPEC's "Proof" lines).
What lives here is only the harness: a node with small, test-friendly limits, and
a client that speaks the AWS JSON protocol to it over ASGI.

`call` is the fixture almost every test will use. It sends one action the way a
real SDK does — `POST /` with an `X-Amz-Target` header — so a test can never
accidentally pass against a route shape that `boto3` would not produce.

> **The boto3 fixture you will want later.** The protocol checklist's real bar is
> "a real AWS SDK works against this unmodified", and boto3 is synchronous and
> needs a socket, so it cannot ride `ASGITransport`. When you get there, add a
> fixture that runs `uvicorn` on an ephemeral port in a thread and hands back a
> `boto3.client("sqs", endpoint_url=...)`. That test is slower than everything
> else here and worth every millisecond — it is the one that finds the places
> where you implemented *your* protocol rather than *the* protocol.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from sqs_queue.config import Settings
from sqs_queue.main import create_app
from sqs_queue.models import QueueAttributes
from sqs_queue.state import AppState, Queue

Call = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def settings() -> Settings:
    """A node with limits small enough to hit deliberately.

    Every value here is chosen so a test can reach the boundary without building
    a load generator: a batch of 11 is over the limit, a 65th waiter is refused,
    and the dedup window closes inside a test's patience.
    """
    return Settings(
        port=9029,
        endpoint_host="localhost:9029",
        # Short enough that a lease-expiry test does not take a coffee break, and
        # long enough that a delete-before-expiry test can still win the race.
        default_visibility_timeout_seconds=0.5,
        max_batch_entries=10,
        max_receive_messages=10,
        # Small, so an OverLimit test can fill it.
        max_waiters=64,
        max_queues=16,
        max_inflight_per_queue=100,
        max_inflight_per_fifo_queue=20,
        dedup_window_seconds=1.0,
        max_dedup_entries=256,
        max_deadlines_per_tick=100,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """A booted node.

    Entering `lifespan_context` runs the real startup path — including the
    deadline loop — so a test can never pass against wiring that would fail in
    production.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def state(app: FastAPI) -> AppState:
    """The assembled runtime behind the app.

    Exposed so a test can set up state directly — creating a queue, inspecting a
    counter — without going through actions that are still `NotImplementedError`.
    """
    runtime = app.state.app_state
    assert isinstance(runtime, AppState)
    return runtime


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sqs") as http:
        yield http


@pytest.fixture
def call(client: httpx.AsyncClient) -> Call:
    """Send one SQS action the way an SDK does."""

    async def _call(action: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await client.post(
            "/",
            headers={
                "x-amz-target": f"AmazonSQS.{action}",
                "content-type": "application/x-amz-json-1.0",
            },
            json=body or {},
        )

    return _call


@pytest.fixture
def make_queue(state: AppState) -> Callable[..., Queue]:
    """Create a queue straight in the store, bypassing the control plane.

    Deliberate: `CreateQueue` is V6 and raises for most of this project's life,
    but every data-plane test needs a queue to exist. Reaching into the store is
    the honest way to get one without pretending V6 is done — and when V6 lands,
    these tests keep working unchanged.
    """

    def _make(name: str = "orders", **overrides: Any) -> Queue:
        return state.store.create(name, QueueAttributes(**overrides))

    return _make
