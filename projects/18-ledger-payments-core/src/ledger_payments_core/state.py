"""The objects every handler and the dispatcher need, assembled once at startup.

In its own module so `routes`, `auth` and `webhooks` can depend on the shape without
importing `main` (which imports all of them — that would be a cycle). The two small
lifecycle helpers live here for the same reason.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Annotated

import asyncpg
from fastapi import Depends
from redis.asyncio import Redis
from starlette.requests import Request

from .config import Settings
from .idempotency import IdempotencyStore
from .isolation import TransferPolicy
from .ledger import Ledger

__all__ = ["AppState", "StateDep", "get_state", "task_failure", "wait_for_shutdown"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    pool: asyncpg.Pool[asyncpg.Record]
    """Owned by the lifespan when it opened it; by the caller (a test) otherwise."""
    redis: Redis
    """A pooled client, not a connection — see `Settings.redis_max_connections`."""
    ledger: Ledger
    """V1: accounts, postings, derived balances."""
    idempotency: IdempotencyStore
    """V3: the Redis cache in front of the Postgres record."""
    policy: TransferPolicy
    """V2's retry bound and amount ceiling, and V4's endpoint."""

    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    """Set exactly once, at shutdown. The dispatcher stops starting rounds when it is;
    the round it is on still settles."""

    background: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    """The webhook dispatcher, when `RUN_DISPATCHER=true`.

    Held here for two reasons. The event loop keeps only a *weak* reference to a task,
    so one nobody else references can be garbage-collected mid-flight — a dispatcher
    silently vanishing while `/healthz` still says `ok`. And shutdown needs the list to
    wait on."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A dependency rather than a module global, so tests can stand up independent apps
    in one process and nothing resolves state at import time, before the lifespan has
    built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
"""`state: StateDep` in a handler or dependency signature injects the `AppState`."""


def task_failure(task: asyncio.Task[None]) -> BaseException | None:
    """What ended `task` — or `None` if it is still running or was cancelled.

    Neither of those is a failure, and asking a live task for its exception raises
    `InvalidStateError` rather than answering.
    """
    if not task.done() or task.cancelled():
        return None
    return task.exception()


async def wait_for_shutdown(shutdown: asyncio.Event, timeout: float) -> bool:
    """Sleep up to `timeout` seconds, waking early the moment `shutdown` is set.

    Returns `True` if shutdown was requested. The difference from `asyncio.sleep` is
    the whole point: an idle dispatcher notices SIGTERM now, not one interval from now.
    """
    try:
        async with asyncio.timeout(timeout):
            await shutdown.wait()
    except TimeoutError:
        return False
    return True
