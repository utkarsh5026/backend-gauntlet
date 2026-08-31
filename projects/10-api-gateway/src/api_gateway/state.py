"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

`router` is deliberately a plain attribute rather than something frozen: V2's
config-reload criterion is satisfied by building a whole new `Router` off the
request path and rebinding this one field. See `router.py` on why a single
attribute assignment is all the synchronization that needs.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Request

from .config import Settings
from .router import Router

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    client: httpx.AsyncClient
    """The process-wide pooled upstream client (V1). One per process, not one per
    request — the connection pool lives inside it, so its lifetime *is* the
    keep-alive reuse the SPEC grades."""
    router: Router
    """Route table (V2) -> upstream pools (V3) -> circuit breakers (V4)."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent gateways in one process (different route tables, different pools),
    and so nothing resolves state at import time, before the lifespan has built
    any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
