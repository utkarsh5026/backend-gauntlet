"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .shard import ShardedIndex

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    engine: ShardedIndex
