"""The objects the gRPC service, the timer loop and the admin routes all need.

Its own module so `service` and `routes` can depend on the shape without
importing `main` (which imports them — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .config import Settings
from .dispatch import Dispatcher
from .history import HistoryStore
from .sticky import StickyCache
from .timers import TimerService

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    """One assembled engine.

    Everything shares the one pool; the sticky cache is the only piece that is
    genuinely per-process state, and that is the point of V5.
    """

    settings: Settings
    pool: asyncpg.Pool[asyncpg.Record]
    history: HistoryStore
    timers: TimerService
    sticky: StickyCache
    dispatcher: Dispatcher
