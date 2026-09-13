"""The app-facing HTTP surface — **wired**, delegates to the vertical modules.

Two audiences share this router:

* **Participants** (`/rooms/*`) — the region-aware signaling API. A publisher
  POSTs its simulcast layers; the room is **placed** (V1 picks a home region via
  consensus if it is new) and this region is registered active. A subscriber POSTs
  the publisher it wants; if that publisher's media lives in another region, a
  **cascade relay leg** (V2) is ensured. The ICE credential exchange and the
  per-subscriber RTP rewrite are **reused from project 15** — the federation is
  what is new here.
* **Peer SFUs** (`/cluster/*`) — the node-to-node Raft-lite RPCs (V1) that keep
  the placement map consistent: `POST /cluster/vote` and `POST /cluster/replicate`.
  Same node-to-node-over-HTTP transport as project 09; the sending half is
  `cluster.ClusterClient`, and both import the bodies from `rpc.py`.

The handlers are thin: validate, call into a vertical, shape the reply. Until a
vertical exists its call raises `NotImplementedError`, which `errors.py` renders
as a 501 naming the todo — so `curl` shows you the worklist.

## Validation is a security control

Every field here arrives from an unauthenticated caller — and on `/cluster/*`,
until the cascade-auth checklist item is done, from *anyone* who can reach the
port. The constraints on the models (and the room-id pattern on the path) are
what keep `ssrc: 2**64` out of a 4-byte wire field, `layers` from growing without
limit, and `../../etc` from ever being a room id that V4 turns into a directory.

TODO(security): authenticate `/cluster/*` (see `cluster.py`) — a dependency on
those two routes is the receiving half.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from .cascade import RelayLeg
from .placement import RoomPlacement
from .rpc import ROOM_ID_PATTERN, ReplicateReply, ReplicateRequest, VoteReply, VoteRequest
from .state import AppState, get_state

__all__ = ["MAX_LAYERS", "router"]

router = APIRouter()

State = Annotated[AppState, Depends(get_state)]
RoomId = Annotated[str, PathParam(pattern=ROOM_ID_PATTERN)]

MAX_LAYERS = 8
"""Most simulcast encodings one publish may announce. Three is the real number."""

UINT32_MAX = 0xFFFFFFFF


class LayerRequest(BaseModel):
    """One announced simulcast encoding (as in project 15)."""

    rid: str = Field(min_length=1, max_length=16)
    ssrc: int = Field(ge=0, le=UINT32_MAX)
    bitrate_bps: int = Field(gt=0, le=100_000_000)


class PublishRequest(BaseModel):
    layers: list[LayerRequest] = Field(min_length=1, max_length=MAX_LAYERS)
    client_ufrag: str = Field(default="", max_length=256)
    """The client's ICE ufrag — consumed by the reused project-15 SFU. Defaulted so
    a `curl` smoke test can publish without one."""


class PublishResponse(BaseModel):
    room: str
    home_region: str
    """The room's single home region, as committed by V1."""
    region: str
    """The region this SFU serves — where this publisher's media enters the mesh."""
    media_addr: str
    layers: list[LayerRequest]


class SubscribeRequest(BaseModel):
    publisher: int = Field(ge=0, le=2**63 - 1)
    """The publisher's peer id (as in project 15)."""
    client_ufrag: str = Field(default="", max_length=256)


class SubscribeResponse(BaseModel):
    room: str
    publisher: int
    region: str
    media_addr: str


class Topology(BaseModel):
    """`GET /rooms` — the web playground's `GlobalTopology`."""

    region: str
    rooms: list[RoomPlacement]
    relay_legs: list[RelayLeg]


# ---- participant signaling -------------------------------------------------


@router.get("/rooms")
async def list_rooms(state: State) -> Topology:
    """The global topology this node agrees on: placements + its own relay legs."""
    return Topology(
        region=state.settings.region,
        rooms=state.placement.snapshot(),
        relay_legs=state.cascade.legs(),
    )


@router.post("/rooms/{room}/publish")
async def publish(room: RoomId, request: PublishRequest, state: State) -> PublishResponse:
    """Announce simulcast layers; place the room (V1) and register this region."""
    region = state.settings.region
    # The first publish for a new room PLACES it: one home region, cluster-wide.
    placement = await state.placement.place_room(room)
    # This region now has a live participant — replicate it, so every node derives
    # the same cascade topology.
    await state.placement.register_interest(room, region, joined=True)
    return PublishResponse(
        room=room,
        home_region=placement.home_region,
        region=region,
        media_addr=state.media_addr,
        layers=request.layers,
    )


@router.post("/rooms/{room}/subscribe")
async def subscribe(room: RoomId, request: SubscribeRequest, state: State) -> SubscribeResponse:
    """Attach to a publisher; ensure a relay leg (V2) if its media lives elsewhere."""
    region = state.settings.region
    await state.placement.register_interest(room, region, joined=True)

    # A read from the locally applied map, not a round-trip to the leader: a
    # subscribe in-region must not block on another continent. A publisher at home
    # needs no leg — its media never leaves the region (no hairpin).
    placement = state.placement.room(room)
    if placement is not None and placement.home_region != region:
        await state.cascade.ensure_leg(placement.home_region, (room, request.publisher))

    return SubscribeResponse(
        room=room,
        publisher=request.publisher,
        region=region,
        media_addr=state.media_addr,
    )


# ---- inter-SFU cluster control (Raft-lite RPCs) ----------------------------


@router.post("/cluster/vote")
async def cluster_vote(request: VoteRequest, state: State) -> VoteReply:
    """A peer's RequestVote. Delegates to placement consensus (V1)."""
    return await state.placement.on_vote(request)


@router.post("/cluster/replicate")
async def cluster_replicate(request: ReplicateRequest, state: State) -> ReplicateReply:
    """A leader's AppendEntries. Delegates to placement consensus (V1)."""
    return await state.placement.on_replicate(request)
