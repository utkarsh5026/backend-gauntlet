"""The admin / observability surface — **wired**, not a vertical.

Liveness and readiness probes (k8s wires `/healthz` to liveness and `/readyz` to
readiness, so a pod only receives traffic once it is fit to serve it) and a
non-secret status blob. `/metrics` is not declared here:
`common_telemetry.metrics_routes()` mounts it in `main`.

## Liveness and readiness are different questions

`/healthz` answers "is this process alive?" — k8s *restarts* the pod when it
fails. `/readyz` answers "should this pod get traffic?" — k8s *removes it from
the Service* when it fails, and restarts nothing. Wiring a dependency check into
liveness is a classic outage amplifier: Postgres blips, every pod fails liveness
at once, and k8s restarts the entire fleet into a database that is already
struggling.

So `/healthz` checks nothing. `/readyz` is 503 when a background task has died;
making it also reflect whether Postgres, Redis and NATS are reachable is on the
Ship-it checklist.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from .state import AppState, get_state

__all__ = ["router"]

router = APIRouter()

State = Annotated[AppState, Depends(get_state)]


@router.get("/healthz", response_class=Response)
async def healthz() -> Response:
    """Liveness: the process is up. Deliberately checks no dependency."""
    return Response("ok", media_type="text/plain")


@router.get("/readyz", response_class=Response)
async def readyz(state: State) -> Response:
    """Readiness: safe to receive traffic. See the module docstring."""
    if state.background_errors:
        return Response("background task stopped", status_code=503, media_type="text/plain")
    return Response("ok", media_type="text/plain")


@router.get("/status")
async def status(state: State) -> dict[str, Any]:
    """A small view of platform config and live topology.

    "Non-secret" is a claim to verify, not a given: `streams` carries each
    session's `stream_key`, which is also the broadcaster's ingest secret. See
    `StreamSession` in `control.py`.
    """
    platform = state.platform
    return {
        "streams_live": platform.live_count,
        "streams": [session.model_dump(mode="json") for session in platform.snapshot()],
        "ladder": [rung.model_dump() for rung in platform.ladder],
        "segment_secs": platform.segment_secs,
        "part_secs": platform.part_secs,
        "transcode": {
            "queue_depth": state.workers.queue_depth,
            "max_replicas": state.workers.max_replicas,
        },
        "chat": {"active_channels": state.chat.active_channels, "node_id": state.chat.node_id},
        "edge_origin": state.edge.origin_base,
        "background_errors": state.background_errors,
    }
