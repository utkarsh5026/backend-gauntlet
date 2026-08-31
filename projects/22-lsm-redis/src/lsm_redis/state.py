"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

Note what is *not* here: nothing about a connection. The RESP server keeps
per-connection state (whether it has authenticated, its read buffer) in the
coroutine serving that connection, where it belongs — a dict of connection state
hanging off shared state would be a leak with a lifecycle bug waiting in it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .engine import Engine

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    engine: Engine
    """The store. One per process — the WAL is a single file with a single
    writer, so two engines over one `data_dir` would be two writers on one log
    and the corruption would be silent until recovery."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent engines in one process over different temp directories, and so
    nothing resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
