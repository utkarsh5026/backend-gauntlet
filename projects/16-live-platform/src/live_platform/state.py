"""The objects every handler needs, assembled once at startup.

In its own module so `routes` and `admin` can depend on the shape without
importing `main` (which imports both — that would be a cycle).

The connection handles live here next to the four planes built on them, so the
lifespan in `main` has one object to tear down and the tests have one object to
build. Nothing here is per-stream: sessions live in the control plane's
registry, subscriptions in the chat hub, in-flight fills in the edge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import asyncpg
import httpx
from nats.aio.client import Client as NatsClient
from redis.asyncio import Redis
from starlette.requests import HTTPConnection

from .chat import ChatHub
from .config import Settings
from .control import ControlPlane
from .edge import EdgeCache
from .workers import WorkerPool

__all__ = ["AppState", "get_state", "task_failure"]


def task_failure(task: asyncio.Task[None]) -> BaseException | None:
    """What ended `task` — or `None` if it is still running or was cancelled.

    Neither of those is a failure, and asking a live task for its exception
    raises `InvalidStateError` rather than answering.
    """
    if not task.done() or task.cancelled():
        return None
    return task.exception()


@dataclass(slots=True)
class AppState:
    settings: Settings

    # --- connection handles (owned by the lifespan when it opened them) ---
    pool: asyncpg.Pool[asyncpg.Record]
    redis: Redis
    nats: NatsClient
    http: httpx.AsyncClient

    # --- the four planes ---
    platform: ControlPlane
    """V1 — session lifecycle over Postgres."""
    workers: WorkerPool
    """V2 — the transcode queue over NATS JetStream."""
    edge: EdgeCache
    """V3 — the LL-HLS edge in front of the packager origin."""
    chat: ChatHub
    """V4 — chat and presence over Redis pub/sub."""

    background: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    """Long-lived tasks started when `RUN_BACKGROUND=true` (the chat bus).

    Held here for two reasons. A task nobody references can be garbage-collected
    mid-flight — the bus silently stopping while `/healthz` still says `ok`. And
    `/readyz` needs to ask whether any of them died."""

    @property
    def background_errors(self) -> list[str]:
        """What killed each dead background task, as `Kind: message`."""
        errors: list[str] = []
        for task in self.background:
            exc = task_failure(task)
            if exc is not None:
                errors.append(f"{task.get_name()}: {type(exc).__name__}: {exc}")
        return errors


def get_state(connection: HTTPConnection) -> AppState:
    """Pull the assembled state off the app, for HTTP routes *and* WebSockets.

    Typed against `HTTPConnection` — the common base of `Request` and
    `WebSocket` — so one dependency serves the playback routes and the chat
    socket alike. A function rather than a module global so tests can stand up
    independent apps in one process, and so nothing resolves state at import
    time, before the lifespan has built any.
    """
    state = getattr(connection.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
