"""The wire shapes of the placement control plane — **wired**, not a vertical.

Two things live here because two modules need them and neither may own them:
the **log entries** of the placement state machine (applied by V1), and the
**node-to-node RPC bodies** that carry them (`/cluster/vote`,
`/cluster/replicate`, sent by `cluster.ClusterClient`, answered by `routes`).

## The state machine's inputs

A replicated log is only as good as the determinism of what it replicates. Two
entry kinds, as a pydantic **discriminated union** on `kind` — the direct
analogue of the Rust `#[serde(tag = "kind")]` enum, and the reason a follower
can decode `entries` without guessing:

* `PlaceRoom` — claim `region` as home for a not-yet-placed room. Consensus
  makes exactly one such claim win per room; applying a later claim for an
  already-placed room must be a no-op, and that no-op is what "idempotent
  placement" means on every node.
* `RegionInterest` — `region` gained its first (`joined=True`) or lost its last
  (`joined=False`) participant in a room. Membership rides the same log so every
  node derives the same cascade topology.

Entries are frozen: an entry, once appended, is a fact. Mutating one in place on
the leader after replicating it is a way to make two nodes disagree about what
index 7 says.

## The RPC bodies are deliberately minimal

`VoteRequest` and `ReplicateRequest` carry who, what term, and (for replicate)
the entries — the same shape the Rust scaffold had, and not enough for a safe
Raft. TODO(V1): extend them. Which fields a vote needs so a stale node cannot
win an election, and which a replicate needs so a follower can detect a gap in
its log and so the leader can tell followers what is safe to apply — project 09
already taught you. Adding them here keeps the client and the handler in lock
step, because both import these classes.

Every bound below is a security control, not tidiness: `/cluster/*` is reachable
by anything that can reach the HTTP port until the cluster-auth checklist item
is done, and `entries` without a `max_length` is an unauthenticated POST that
grows the log without limit.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_ENTRIES_PER_RPC",
    "ROOM_ID_PATTERN",
    "PlaceRoom",
    "PlacementEntry",
    "RegionInterest",
    "ReplicateReply",
    "ReplicateRequest",
    "VoteReply",
    "VoteRequest",
]

ROOM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
"""What a room id may look like. Bounded length, a boring alphabet, and an
alphanumeric first character: a room id ends up in the replicated log, in log
lines and (V4) as a directory name under `RECORDING_DIR` — so `..` and `.hidden`
must never be rooms, and a slash can never reach the pattern at all."""

MAX_ENTRIES_PER_RPC = 256
"""Most log entries one replicate call may carry — the per-RPC half of "the
replicated log is bounded". A lagging follower catches up over several calls."""

RoomId = Annotated[str, Field(pattern=ROOM_ID_PATTERN)]
RegionName = Annotated[str, Field(min_length=1, max_length=64)]
NodeName = Annotated[str, Field(min_length=1, max_length=64)]


class PlaceRoom(BaseModel):
    """Claim `region` as the home of `room_id`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["place_room"] = "place_room"
    room_id: RoomId
    region: RegionName


class RegionInterest(BaseModel):
    """`region` gained its first / lost its last participant in `room_id`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["region_interest"] = "region_interest"
    room_id: RoomId
    region: RegionName
    joined: bool


PlacementEntry = Annotated[PlaceRoom | RegionInterest, Field(discriminator="kind")]
"""One entry in the replicated placement log."""


class VoteRequest(BaseModel):
    """`POST /cluster/vote` — a candidate asks for this node's vote."""

    candidate_id: NodeName
    term: int = Field(ge=0)


class VoteReply(BaseModel):
    granted: bool
    term: int = Field(ge=0)


class ReplicateRequest(BaseModel):
    """`POST /cluster/replicate` — a leader appends entries (empty = heartbeat)."""

    leader_id: NodeName
    term: int = Field(ge=0)
    entries: list[PlacementEntry] = Field(
        default_factory=list[PlacementEntry], max_length=MAX_ENTRIES_PER_RPC
    )


class ReplicateReply(BaseModel):
    success: bool
    term: int = Field(ge=0)
