"""The objects every handler needs, assembled once at startup.

In its own module so `routes` can depend on the shape without importing `main`
(which imports `routes` — that would be a cycle).

Note what is *not* here: nothing about a peer connection. Per-peer state — the
choke flags, the read buffer, whether it holds an upload slot — lives in the
coroutine serving that peer, where it belongs. A dict of peer state hanging off
shared state would be a leak with a lifecycle bug waiting in it, and it would
need locking that the per-task version does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .client import Client
from .config import Settings
from .seeder import Seeder


@dataclass(slots=True)
class AppState:
    settings: Settings
    client: Client

    seeder: Seeder | None = None
    """The inbound-peer listener, present only when `RUN_SEEDER=true`.

    `None` is a real state, not a missing value: a leech-only client is a
    perfectly good BitTorrent client, and it is the scaffold's default so that
    the bare app serves the control plane without an accept loop raising on its
    first inbound peer."""


def get_state(request: Request) -> AppState:
    """Pull the assembled state off the app.

    A function rather than a module-level global so tests can stand up several
    independent clients in one process over different temp directories, and so
    nothing resolves state at import time, before the lifespan has built any.
    """
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state
