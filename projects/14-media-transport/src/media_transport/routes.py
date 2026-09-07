"""The admin / observability HTTP surface — **wired**, not a vertical.

The media plane is UDP. This small FastAPI app exists only for liveness and
readiness probes and a non-secret status blob; `/metrics` is not declared here
because `common_telemetry.metrics_routes()` mounts it in `main`.

There is no signaling API and nothing here takes input, which is why this module
is short and why `errors.py` spends its length on the media plane instead. The
one interesting decision is `/readyz`.

## Liveness and readiness are not the same question here

`/healthz` answers "is this process alive". `/readyz` answers "is it doing its
job", and in this project those diverge constantly and on purpose: the session
task dies the moment a vertical raises `NotImplementedError`, and everything
HTTP keeps working. If both endpoints returned `ok` you would have a transport
that passes every probe, moves no packets, and gives you nothing to look at but
a flat `/metrics`.

So `/readyz` is 503 when the session task is done. On the bare scaffold that is
the *expected* state after the first datagram, and it is the fastest way to see
which vertical you are on — the status blob names the exception.
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
    """Liveness: the process is up and serving. Says nothing about media."""
    return Response("ok", media_type="text/plain")


@router.get("/readyz", response_class=Response)
async def readyz(state: State) -> Response:
    """Readiness: the media session is still running. See the module docstring."""
    if state.session.done():
        return Response("session stopped", status_code=503, media_type="text/plain")
    return Response("ok", media_type="text/plain")


@router.get("/status")
async def status(state: State) -> dict[str, Any]:
    """A small, non-secret view of how this process is configured and doing.

    `session_error` is the useful field on a scaffold: when a vertical raises,
    this is where the exception's message surfaces without going to the log,
    unwrapped from the `TaskGroup` nesting so it names the vertical rather than
    the group. See `state.session_failure`.
    """
    config = state.settings
    return {
        "role": config.role.value,
        "rtp_addr": f"{state.media.local_addr[0]}:{state.media.local_addr[1]}",
        "remote_addr": config.remote_addr,
        "session_running": not state.session.done(),
        "session_error": state.session_error,
        "datagrams_dropped": state.media.dropped,
        "mtu": config.mtu,
        "payload_type": config.payload_type,
        "playout_ms": config.playout_ms,
        "bounds": {
            "jitter_capacity": config.jitter_capacity,
            "rtx_cache_packets": config.rtx_cache_packets,
            "rtp_inbox": config.rtp_inbox,
        },
        "bitrate_bps": {
            "min": config.min_bitrate,
            "start": config.start_bitrate,
            "max": config.max_bitrate,
        },
    }
