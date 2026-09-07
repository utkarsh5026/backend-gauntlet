"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

Note what is *not* here: nothing per-stream. The jitter buffer, the packetizer,
the retransmit cache and the congestion controller all live inside the session
coroutines in `session.py`, reached only from there. That is not an oversight —
they are mutated on every packet, and a second place holding references to them
is how an admin endpoint ends up reading a half-updated buffer, or worse,
keeping one alive after its stream is gone.

What the admin plane gets instead is the *socket* and the *task*, because those
answer the two questions a probe actually has: is the media plane bound, and is
anything still running on it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from fastapi import Request

from .config import Settings
from .udp import MediaSocket

__all__ = ["AppState", "get_state", "session_failure", "unwrap_group"]


def unwrap_group(exc: BaseException | None) -> BaseException | None:
    """Dig the real exception out of any `TaskGroup` nesting around it.

    The session runs its concerns in an `asyncio.TaskGroup`, so a vertical's
    `NotImplementedError` arrives wrapped in an `ExceptionGroup`. That is
    correct behaviour and completely useless to read: "unhandled errors in a
    TaskGroup (1 sub-exception)" names neither the vertical nor the function.

    Every place that reports a dead session — the log line when it dies, the
    warning at shutdown, the `/status` blob — goes through here, so they can
    never disagree about what killed it.
    """
    while isinstance(exc, BaseExceptionGroup):
        # `isinstance` against a generic alias narrows to
        # `BaseExceptionGroup[Unknown]`, which pyright strict rejects. The cast
        # states what the runtime check already established.
        group = cast(BaseExceptionGroup[BaseException], exc)
        if not group.exceptions:
            return group
        exc = group.exceptions[0]
    return exc


def session_failure(task: asyncio.Task[None]) -> BaseException | None:
    """What ended `task`, unwrapped — or `None` if it did not fail.

    `None` for a task that is still running or was cancelled: neither is a
    failure, and asking a live task for its exception raises `InvalidStateError`
    rather than answering.
    """
    if not task.done() or task.cancelled():
        return None
    return unwrap_group(task.exception())


@dataclass(slots=True)
class AppState:
    settings: Settings
    media: MediaSocket
    session: asyncio.Task[None]
    """The transport session.

    Held here so `/readyz` can ask whether it is still running. That question
    has a real answer in this project rather than a formal one: the session dies
    the first time a vertical raises `NotImplementedError`, and the process goes
    on serving `/healthz` and `/metrics` perfectly while moving no media at all.
    A liveness probe cannot tell those apart. A readiness probe can, and should
    — an orchestrator that keeps routing to a transport whose media plane is
    dead is the exact failure this endpoint exists to prevent.
    """

    @property
    def session_error(self) -> str | None:
        """What killed the session, or `None` while it is running.

        On a scaffold this is the single most useful field on `/status`: it
        names the vertical you are on without going to the log.
        """
        exc = session_failure(self.session)
        return None if exc is None else f"{type(exc).__name__}: {exc}"


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent transports in one process on ephemeral ports, and so nothing
    resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
