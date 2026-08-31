"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .node import RaftNode

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    node: RaftNode


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can run several
    independent nodes in one process — which this project needs more than most,
    since a meaningful test *is* a three-node cluster — and so nothing resolves
    state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
