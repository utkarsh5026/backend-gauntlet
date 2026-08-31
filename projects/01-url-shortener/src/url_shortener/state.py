"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` - that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from fastapi import Request

from .cache import Cache
from .config import Settings
from .id_gen import IdGenerator
from .ingest import ClickSink
from .ratelimit import RateLimiter

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    pool: asyncpg.Pool[asyncpg.Record]
    cache: Cache
    ids: IdGenerator
    clicks: ClickSink
    limiter: RateLimiter

    @property
    def api_keys(self) -> set[str]:
        return self.settings.api_keys

    @property
    def base_url(self) -> str:
        return self.settings.base_url


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can run several
    independent apps in one process - and so nothing resolves state at import
    time, before the lifespan has built any.
    """
    state: AppState = request.app.state.app_state
    return state
