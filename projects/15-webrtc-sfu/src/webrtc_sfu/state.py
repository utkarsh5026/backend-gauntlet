"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

Note what is *not* here: nothing per-peer. A peer's ICE agent, rewriter,
selector and estimator live inside the `Sfu` core, reached through it, because
the media plane mutates them on every packet and a second place holding
references to the same objects is a lifecycle bug waiting for a peer to leave.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .pump import MediaSocket
from .sfu import Sfu

__all__ = ["AppState", "get_state"]


@dataclass(slots=True)
class AppState:
    settings: Settings
    sfu: Sfu
    media: MediaSocket
    pump: asyncio.Task[None]
    """The media pump.

    Held here so `/readyz` can ask whether it is still running. That question
    has a real answer in this project rather than a formal one: the pump dies
    the first time a vertical raises `NotImplementedError`, and the process goes
    on serving signaling perfectly while forwarding nothing at all. A liveness
    probe cannot tell those apart. A readiness probe can, and should — an
    orchestrator routing new peers to an SFU whose media plane is dead is the
    exact failure this endpoint exists to prevent.
    """


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent SFUs in one process on ephemeral ports, and so nothing resolves
    state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
