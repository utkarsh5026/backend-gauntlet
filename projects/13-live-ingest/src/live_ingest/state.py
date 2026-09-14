"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

The registry is the only thing both planes share: RTMP sessions write parts
into it, HTTP handlers read them back. The ingest server is here too, but only
so `/status` can report on it — a handler never reaches into a session.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .ingest import RtmpIngest
from .live import LiveRegistry

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    registry: LiveRegistry
    ingest: RtmpIngest


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can run several
    independent servers in one process on ephemeral ports, and so nothing
    resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
