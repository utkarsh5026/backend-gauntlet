"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .catalog import Catalog
from .config import Settings

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    catalog: Catalog
    """The scanned media library and the packaging pipeline hanging off it.

    Built once in the lifespan. Scanning is directory I/O, so it happens before
    the server accepts a connection rather than lazily on the first request —
    which also means `GET /assets` is honest the moment the port is open."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent servers in one process over different media directories, and so
    nothing resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
