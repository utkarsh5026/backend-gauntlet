"""The admin / observability HTTP surface — **wired**, not a vertical.

Liveness, readiness, and a non-secret status blob: this node's identity and role
in the mesh, the placed rooms, the open relay legs, the per-leg layer sets and the
active recordings — enough to watch a room's cascade tree form and a leader
election settle by polling three nodes. `/metrics` is mounted in `main` from
`common_telemetry.metrics_routes()`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from .cascade import RelayLeg
from .placement import Role, RoomPlacement
from .recording import ActiveRecording
from .routing import LegLayers
from .state import AppState, get_state

__all__ = ["router"]

router = APIRouter()

State = Annotated[AppState, Depends(get_state)]


class NodeStatus(BaseModel):
    region: str
    node_id: str
    media_addr: str
    cascade_addr: str
    peers: list[str]
    max_peers_per_room: int


class PlacementStatus(BaseModel):
    role: Role
    term: int
    leader: str | None
    max_rooms: int
    quorum: int
    rooms: list[RoomPlacement]


class CascadeStatus(BaseModel):
    max_legs: int
    legs: list[RelayLeg]


class RoutingStatus(BaseModel):
    hysteresis_ticks: int
    legs: list[LegLayers]


class RecordingStatus(BaseModel):
    segment_secs: float
    active: list[ActiveRecording]


class PlaneStatus(BaseModel):
    backbone_pump_running: bool
    election_running: bool | None
    """`None` when `RUN_BACKGROUND` is off and there is no election loop at all."""
    media_datagrams_dropped: int
    cascade_datagrams_dropped: int


class StatusResponse(BaseModel):
    node: NodeStatus
    placement: PlacementStatus
    cascade: CascadeStatus
    routing: RoutingStatus
    recording: RecordingStatus
    planes: PlaneStatus


@router.get("/healthz", response_class=Response)
async def healthz() -> Response:
    """Liveness: the process is up and the event loop is turning."""
    return Response(content="ok", media_type="text/plain")


@router.get("/readyz", response_class=Response)
async def readyz(state: State) -> Response:
    """Readiness: up **and the backbone is actually being drained**.

    TODO(observability): once V1 works, readiness should also reflect whether this
    node can reach a quorum. A minority node still serves committed rooms, but it
    cannot *place* one, and a load balancer choosing where to send a brand-new
    meeting should know that.
    """
    if state.backbone_pump.done():
        return Response(
            content="backbone pump not running", status_code=503, media_type="text/plain"
        )
    return Response(content="ready", media_type="text/plain")


@router.get("/status")
async def status(state: State) -> StatusResponse:
    config = state.settings
    placement = state.placement
    election = state.election
    return StatusResponse(
        node=NodeStatus(
            region=config.region,
            node_id=config.node_id,
            media_addr=state.media_addr,
            cascade_addr=config.advertised_media_addr(state.backbone.local_addr[1]),
            peers=[peer.region for peer in config.peer_nodes],
            max_peers_per_room=config.max_peers_per_room,
        ),
        placement=PlacementStatus(
            role=placement.role,
            term=placement.term,
            leader=placement.leader,
            max_rooms=placement.config.max_rooms,
            quorum=placement.config.quorum,
            rooms=placement.snapshot(),
        ),
        cascade=CascadeStatus(max_legs=state.cascade.config.max_legs, legs=state.cascade.legs()),
        routing=RoutingStatus(
            hysteresis_ticks=state.routing.config.hysteresis_ticks,
            legs=state.routing.snapshot(),
        ),
        recording=RecordingStatus(
            segment_secs=state.recorder.config.segment_secs,
            active=state.recorder.active(),
        ),
        planes=PlaneStatus(
            backbone_pump_running=not state.backbone_pump.done(),
            election_running=None if election is None else not election.done(),
            media_datagrams_dropped=state.media.dropped,
            cascade_datagrams_dropped=state.backbone.dropped,
        ),
    )
