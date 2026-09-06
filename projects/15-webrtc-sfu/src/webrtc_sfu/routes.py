"""The signaling + admin HTTP surface — **wired**, not a vertical.

Two things share one server, because they are both "HTTP that is not media".

**Signaling.** Before any media flows the two sides have to agree on *who is
talking to whom* and exchange ICE credentials. In real WebRTC that is an SDP
offer/answer carried over any channel you like; here it is a small JSON API that
does the same job without the SDP grammar (parsing full SDP is a documented
stretch, not the learning). A publisher POSTs its simulcast layers and gets back
ICE credentials plus the media address to connect to; a subscriber POSTs the
publisher it wants and gets its own credentials plus the stable SSRC it will
receive on. Those calls build the room/peer graph the `Sfu` core forwards over.

**Admin.** Liveness, readiness, a non-secret status blob. `/metrics` is not here
— `common_telemetry.metrics_routes()` mounts it in `main`.

## Validation is a security control here, not a formality

Every field below arrives from an unauthenticated caller. pydantic's constraints
are what stop `ssrc: 2**64` from reaching a `struct.pack(">I", ...)` four layers
down, and what bounds `layers` so a single POST cannot grow the SFU's SSRC
routing table without limit. Doing it in the model means it happens before the
handler runs, once, in one place — which is the whole reason the request models
are declared rather than the body read as a dict.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from .sfu import PeerHandle, SimulcastLayer, Topology
from .state import AppState, get_state

__all__ = ["MAX_LAYERS", "router"]

router = APIRouter()

MAX_LAYERS = 8
"""Most simulcast encodings a publisher may announce in one call.

Three is the real-world number (low/mid/high). The cap is here because each
layer claims an entry in the SFU's SSRC routing table and an unauthenticated
POST should never be able to claim an unbounded number of them — the
"bounded everything" checklist item, applied at the door.
"""

State = Annotated[AppState, Depends(get_state)]

UINT32_MAX = 0xFFFFFFFF


class LayerRequest(BaseModel):
    """One announced simulcast encoding in a publish request."""

    rid: str = Field(max_length=16)
    """RTP stream id, as the publisher labels it ("q"/"h"/"f")."""

    ssrc: int = Field(ge=0, le=UINT32_MAX)
    """Constrained to a real u32 — this ends up in a 4-byte wire field."""

    bitrate_bps: int = Field(gt=0, le=100_000_000)
    """Nominal cost of receiving this layer. The ceiling is a sanity bound: a
    layer claiming 10 Gbps would make every budget comparison in V3 meaningless.
    """


class PublishRequest(BaseModel):
    layers: list[LayerRequest] = Field(min_length=1, max_length=MAX_LAYERS)
    client_ufrag: str = Field(default="", max_length=256)
    """The client's own ICE ufrag — the remote half of the exchange. Defaulted
    so a `curl` smoke test can create a publisher without one; a real client
    always sends it, and V1's USERNAME check is what makes it matter."""


class SubscribeRequest(BaseModel):
    publisher: int = Field(ge=0)
    client_ufrag: str = Field(default="", max_length=256)


class StatusResponse(BaseModel):
    """A small non-secret view of how this SFU is configured and what it holds."""

    media_addr: str
    limits: dict[str, int]
    bitrate_bps: dict[str, int]
    media_pump_running: bool
    media_datagrams_dropped: int
    topology: Topology


@router.get("/healthz", response_class=Response)
async def healthz() -> Response:
    """Liveness: the process is up and the event loop is turning."""
    return Response(content="ok", media_type="text/plain")


@router.get("/readyz", response_class=Response)
async def readyz(state: State) -> Response:
    """Readiness: the process is up **and the media plane is actually running**.

    Deliberately not an alias for `/healthz`. On the bare scaffold this is the
    endpoint that tells the truth: the first STUN check a browser sends kills
    the pump on V1's `NotImplementedError`, signaling keeps answering, and only
    this turns red. See `state.AppState.pump`.
    """
    if state.pump.done():
        return Response(content="media pump not running", status_code=503, media_type="text/plain")
    return Response(content="ready", media_type="text/plain")


@router.get("/status")
async def status(state: State) -> StatusResponse:
    config = state.settings
    return StatusResponse(
        media_addr=f"{config.public_ip}:{config.media_port}",
        limits={
            "max_rooms": config.max_rooms,
            "max_peers_per_room": config.max_peers_per_room,
            "media_inbox": config.media_inbox,
        },
        bitrate_bps={
            "min": config.min_bitrate,
            "start": config.start_bitrate,
            "max": config.max_bitrate,
        },
        media_pump_running=not state.pump.done(),
        media_datagrams_dropped=state.media.dropped,
        topology=state.sfu.topology(),
    )


@router.get("/rooms")
async def list_rooms(state: State) -> Topology:
    """The live topology — peer ids and roles only, never credentials."""
    return state.sfu.topology()


@router.post("/rooms/{room}/publish", response_model_exclude_none=True)
async def publish(room: str, request: PublishRequest, state: State) -> PeerHandle:
    """Announce simulcast layers; get ICE credentials + the media address."""
    layers = [
        SimulcastLayer(rid=layer.rid, ssrc=layer.ssrc, bitrate_bps=layer.bitrate_bps)
        for layer in request.layers
    ]
    return state.sfu.join_publisher(room, layers, request.client_ufrag)


@router.post("/rooms/{room}/subscribe", response_model_exclude_none=True)
async def subscribe(room: str, request: SubscribeRequest, state: State) -> PeerHandle:
    """Attach to a publisher; get ICE credentials + the stable outbound SSRC."""
    return state.sfu.subscribe(room, request.publisher, request.client_ufrag)
