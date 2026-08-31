"""The HTTP sidecar: health, stats, and the Prometheus scrape.

This is **not** the data plane. Clients read and write over RESP (V1) on the
redis port; this tiny FastAPI surface is how you *observe* the engine. It is
fully wired and answers on the bare scaffold — before any vertical exists — so
you can watch memtable bytes climb and the SSTable count move while you build
the store, which is a much better feedback loop than reading your own logs.

Keeping the two on separate ports is deliberate and is what real databases do:
a scrape or a health probe can never contend with a read, and `/metrics` can
stay unauthenticated on a port you do not expose while the data port is the one
with `AUTH` on it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from .engine import EngineStats
from .state import AppState, get_state

__all__ = ["router"]

StateDep = Annotated[AppState, Depends(get_state)]

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only.

    Deliberately not a readiness check, and the distinction has teeth for a
    storage engine: this answers `ok` while a compaction is behind, while the
    memtable is full, while the disk is nearly out. Wiring an orchestrator to
    restart the process on any of those would take a degraded store and turn it
    into an unavailable one — and a restart during a write stall costs you a
    full WAL replay on the way back, which makes the outage longer, not shorter.

    If you want a readiness signal, build it from `/stats` and say explicitly
    what "not ready" means. "The store is unhappy" and "the store should be
    killed" are different claims.
    """
    return {"status": "ok"}


@router.get("/stats")
async def stats(state: StateDep) -> EngineStats:
    """A snapshot of engine internals.

    The observability checklist grades this on *movement*, not on existence:
    memtable bytes must rise on writes and drop on flush, the SSTable count must
    rise on flush and fall on compaction. Those transitions are the proof that
    the numbers are wired to the thing they claim to describe.
    """
    return state.engine.stats()


@router.get("/config")
async def config(state: StateDep) -> dict[str, Any]:
    """The tunables this process is running with.

    Worth having because every number in `docs/22-benchmarks.md` is meaningless
    without the configuration that produced it, and asking the running process
    beats trusting a `.env` you think you remember editing.

    `REQUIREPASS` is absent — see `Settings.public_stats`, which enumerates what
    is safe rather than removing what is not, and the smoke test that keeps it
    that way.
    """
    return state.settings.public_stats()
