"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from .broker import Broker
from .config import Settings

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    broker: Broker

    @property
    def max_record_bytes(self) -> int:
        return self.settings.max_record_bytes
