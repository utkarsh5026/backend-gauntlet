"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from .backpressure import Mailbox, OverflowPolicy
from .cluster import ClusterBridge
from .config import Settings
from .directory import Directory
from .hub import Hub
from .presence import PresenceRegistry

__all__ = ["AppState"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    hub: Hub
    """The in-process fan-out hub (V1)."""
    presence: PresenceRegistry
    """Per-topic presence (V3)."""
    cluster: ClusterBridge | None
    """The cross-node bus (V4). `None` in single-node mode (`CLUSTER=false`)."""
    directory: Directory | None
    """The persistent roster behind the admin panel. `None` when `DATABASE_URL`
    is unset — the pub/sub core runs DB-free; only `/admin` needs this.
    Playground scaffolding, not a vertical."""

    def new_mailbox(self) -> Mailbox:
        """A bounded outbox for one new connection, sized and policed by config."""
        return Mailbox(self.settings.outbox_capacity, self.settings.overflow_policy)

    @property
    def overflow_policy(self) -> OverflowPolicy:
        return self.settings.overflow_policy
