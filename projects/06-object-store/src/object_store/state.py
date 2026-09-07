"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

Rust called this `AppState` and cloned it into every handler because each field
sat behind an `Arc`. Python has no such ceremony: this is one object, handlers
reach it off `request.app.state`, and the reference count is the runtime's
problem.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .auth import AuthConfig
from .config import Settings
from .index_backend import IndexBackend
from .lifecycle import Lifecycle
from .multipart import Multipart
from .store import Store

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    """Everything a request handler is allowed to reach."""

    settings: Settings
    store: Store
    """V1 — the content-addressed blob store."""
    index: IndexBackend
    """V3 — the `(bucket, key) → blob` map, local or remote."""
    multipart: Multipart
    """V4 — in-progress upload sessions."""
    lifecycle: Lifecycle
    """Ageing rules and the tier-aware read path."""
    auth: AuthConfig | None
    """`None` means the object routes are open — see `auth` on why an unset
    secret is the off switch rather than a separate flag."""

    @property
    def max_object_size(self) -> int:
        return self.settings.max_object_size


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent stores in one process over different data dirs, and so nothing
    resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
