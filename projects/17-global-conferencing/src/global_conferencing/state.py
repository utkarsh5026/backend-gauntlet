"""The objects every handler needs, assembled once in the lifespan.

In its own module so `routes` and `admin` can depend on the shape without
importing `main` (which imports them — a cycle).

The four vertical objects are held here and nowhere else. `CascadeMesh` holds a
reference to the `LayerRouter` because V2 consults V3 per packet; nothing else
holds a second reference to any of them, so there is exactly one place a room's
placement, a leg's layer set or a recording's segment state can live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import Request

from .cascade import CascadeMesh
from .cluster import ClusterClient
from .config import Settings
from .placement import Placement
from .recording import Recorder
from .routing import LayerRouter
from .transport import UdpEndpoint

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    placement: Placement
    cascade: CascadeMesh
    routing: LayerRouter
    recorder: Recorder
    cluster: ClusterClient
    media: UdpEndpoint
    backbone: UdpEndpoint
    backbone_pump: asyncio.Task[None]
    """Held so `/readyz` can ask whether it is still running. On the bare scaffold
    the first relay datagram ends it with V2's `NotImplementedError` while signaling
    keeps answering — a liveness probe cannot tell those apart; readiness can."""

    election: asyncio.Task[None] | None
    """The placement election loop, or `None` when `RUN_BACKGROUND` is off."""

    @property
    def media_addr(self) -> str:
        """The ICE host candidate advertised to local clients, on the port actually bound."""
        return self.settings.advertised_media_addr(self.media.local_addr[1])


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can run several
    independent SFUs — a whole mesh — in one process on ephemeral ports.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
