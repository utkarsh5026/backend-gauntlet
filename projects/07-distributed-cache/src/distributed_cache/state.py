"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .coordinator import Coordinator
from .membership import Membership
from .store import Store

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    store: Store
    membership: Membership
    coordinator: Coordinator

    @property
    def max_value_bytes(self) -> int:
        return self.settings.max_value_bytes
