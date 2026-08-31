"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .queue import Queue

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    queue: Queue

    @property
    def enqueue_token(self) -> str | None:
        """The token the mutating routes require, or `None` when auth is disabled."""
        return self.settings.token


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can run several
    independent apps in one process — and so nothing resolves state at import time,
    before the lifespan has built any.
    """
    state: AppState = request.app.state.app_state
    return state
