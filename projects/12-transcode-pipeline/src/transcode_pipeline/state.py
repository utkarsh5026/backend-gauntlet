"""The objects every handler and background loop needs, assembled once at startup.

In its own module so `routes`, `dag` and `worker` can depend on the shape without
importing `main` (which imports all three — that would be a cycle). The two small
lifecycle helpers live here for the same reason: the scheduler and the workers
both need them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import asyncpg
from starlette.requests import Request

from .config import Settings
from .store import JobStore
from .workdir import WorkDir

__all__ = ["AppState", "get_state", "task_failure", "wait_for_shutdown"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    pool: asyncpg.Pool[asyncpg.Record]
    """Owned by the lifespan when it opened it; by the caller (a test) otherwise."""
    store: JobStore
    """The durable DAG — V2's rows and V3's claim."""
    workdir: WorkDir
    """The artifact layout under `WORK_DIR`."""

    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    """Set exactly once, at shutdown. The scheduler stops ticking and every worker
    stops *claiming* when it is — in-flight tasks still finish and settle."""

    background: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    """The scheduler and the workers, when `RUN_WORKERS=true`.

    Held here for two reasons. The event loop keeps only a *weak* reference to a
    task, so one nobody else references can be garbage-collected mid-flight — a
    worker silently vanishing while `/healthz` still says `ok`. And shutdown needs
    the list to wait on."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A dependency rather than a module global, so tests can stand up independent
    apps in one process and nothing resolves state at import time, before the
    lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


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

    Returns `True` if shutdown was requested. The difference from `asyncio.sleep`
    is the whole point: an idle worker notices SIGTERM now, not one poll interval
    from now — which, multiplied across a pool, is the difference between a drain
    that finishes inside the grace period and one that doesn't.
    """
    try:
        async with asyncio.timeout(timeout):
            await shutdown.wait()
    except TimeoutError:
        return False
    return True
