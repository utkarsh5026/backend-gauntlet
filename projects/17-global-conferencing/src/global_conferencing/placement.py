"""V1 — Global room placement via consensus. `src/global_conferencing/placement.py`.

This is the control plane every SFU in the mesh must agree on. When a room is
first created, *some* region is chosen as its **home** — the anchor where the
publisher's origin media lives and where the cascade tree (V2/V3) roots — and
that decision must be **identical on every node**, or two people creating
`standup-42` from Tokyo and Frankfurt at the same instant get two disjoint
conferences under one id: a *split room*, conferencing's split-brain. Deciding
one value, cluster-wide, under concurrency and partition, is a **consensus**
problem — exactly what your Raft work in **project 09** is for.

You are **leaning on** project 09's ideas — a randomized election timeout to
elect a leader, append-entries to replicate, a commit index, idempotent apply —
not rebuilding Raft from nothing. What is *yours* here is modelling **room
placement + region membership** as the replicated state machine (the entries in
`rpc.py`) and getting the safety property right: **at most one home region per
room, cluster-wide, forever** — and a minority partition that *cannot* invent one.

## Why this is not a cache, even inside one process

There is no lock in this class and no thread, because one event loop runs every
handler. That does not make it race-free. Every `await` is a point where another
coroutine runs, so

    if room_id not in self._rooms:
        await <replicate a PlaceRoom>        # <- two publishes both get here
        self._rooms[room_id] = ...

lets two concurrent `publish` requests *on the same node* both pass the check and
both propose a home. That is the split room, reproduced without a network. What
serializes the decision — a log index, an `asyncio.Lock`, both — and which of
those you are relying on for *cross-node* safety, belongs in `docs/17-design.md`.

## The two consistency tiers

Placement is strongly consistent; the media riding on it (V2) is best-effort and
drops packets freely. Hot routing reads — `room()`, `snapshot()` — come from the
**locally applied** map, never a round-trip to the leader: a subscribe in Tokyo
must not block on Frankfurt. Those reads are wired below. Everything that
*changes* the map is the V1 worklist.

Scaffold state: construction and the read-only views are wired so `/status` and
`GET /rooms` answer with an empty map. The first `publish` reaches `place_room`
and raises — that is the worklist, rendered by the server as a 501.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer

from .cluster import ClusterClient
from .config import PeerNode
from .rpc import ReplicateReply, ReplicateRequest, VoteReply, VoteRequest

__all__ = ["Placement", "PlacementConfig", "Role", "RoomPlacement"]


class Role(StrEnum):
    """This node's role in the placement group. The same three states as project 09."""

    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"

    @property
    def gauge_value(self) -> int:
        """Encoding for the `conf_node_role` gauge: 0 follower, 1 candidate, 2 leader."""
        return {Role.FOLLOWER: 0, Role.CANDIDATE: 1, Role.LEADER: 2}[self]


class RoomPlacement(BaseModel):
    """The committed placement of one room — the state machine's value for it.

    Every node converges to the same `RoomPlacement` for a given `room_id`. Frozen,
    because a committed placement is a fact: applying an entry produces a *new*
    value (`model_copy(update=...)`) rather than editing one another coroutine
    may be holding mid-serialization.
    """

    model_config = ConfigDict(frozen=True)

    room_id: str
    home_region: str
    """The one home region, chosen once by consensus. The invariant V1 protects."""

    active_regions: frozenset[str] = frozenset()
    """Regions with ≥1 live participant — the cascade topology is derived from this."""

    epoch: int = 0
    """Bumped on each committed change to this room (membership included). Re-placing
    an already-placed room must not bump it — that is the "no epoch churn" criterion."""

    @field_serializer("active_regions")
    def _regions_sorted(self, regions: frozenset[str]) -> list[str]:
        # A set has no order; JSON does. Sorting here means two nodes holding the
        # same placement render byte-identical JSON, which is what lets a test
        # (or you, eyeballing three `/rooms` responses) compare them directly.
        return sorted(regions)


@dataclass(frozen=True, slots=True)
class PlacementConfig:
    """The placement group's fixed parameters, built from `Settings` in `main`."""

    region: str
    """This node's region — the home it proposes for rooms created locally."""

    node_id: str
    """This node's stable id, used in vote records and logs."""

    peers: tuple[PeerNode, ...]
    """The other SFUs in the mesh."""

    election_timeout: float
    """Seconds. A follower stands after a random interval in
    `[election_timeout, 2 * election_timeout)` — project 09's split-vote breaker."""

    heartbeat: float
    """Seconds between leader heartbeats; below `election_timeout` (checked in config)."""

    max_rooms: int
    """Cluster-wide cap on placed rooms, enforced through the log — not per node."""

    @property
    def cluster_size(self) -> int:
        return len(self.peers) + 1

    @property
    def quorum(self) -> int:
        """Votes needed to decide anything: a strict majority of the mesh, self included.

        A node that cannot gather this many answers is in a minority and must refuse
        to place a room.
        """
        return self.cluster_size // 2 + 1


class Placement:
    """The placement control plane: a Raft-lite replicated map of room → home + membership."""

    def __init__(self, config: PlacementConfig, cluster: ClusterClient) -> None:
        self.config = config
        self._cluster = cluster
        self._role = Role.FOLLOWER
        self._term = 0
        self._leader: str | None = None
        self._rooms: dict[str, RoomPlacement] = {}
        """The applied state machine: room id → committed placement. Rebuilt by
        applying committed log entries in order; the source of truth for routing reads."""

        # TODO(V1): the rest of the replicated state. Roughly: the log itself
        # (a `list` of `PlacementEntry`, where the index *is* the position), the
        # commit index and the last-applied index, who this node voted for this
        # term, and something the replicate handler can poke to reset the election
        # timer (an `asyncio.Event` is one shape). Project 09 already taught you
        # which of these must survive a restart — the "a node that rejoins learns
        # the committed placements" criterion is where that comes due.

    # ---- wired read views --------------------------------------------------

    @property
    def role(self) -> Role:
        return self._role

    @property
    def term(self) -> int:
        return self._term

    @property
    def leader(self) -> str | None:
        """Who this node believes leads the placement group, if anyone."""
        return self._leader

    def snapshot(self) -> list[RoomPlacement]:
        """Every placed room, from the locally applied map (for `/rooms` + `/status`)."""
        return [self._rooms[room_id] for room_id in sorted(self._rooms)]

    def room(self, room_id: str) -> RoomPlacement | None:
        """The committed placement for one room, if placed — the hot routing read."""
        return self._rooms.get(room_id)

    # ---- V1 worklist: elect · replicate · apply ----------------------------

    async def place_room(self, room_id: str) -> RoomPlacement:
        """Ensure `room_id` has a home region, cluster-wide, and return its placement.

        TODO(V1): if the room is already placed, return that placement — no second
        home, no epoch bump (idempotent). Otherwise propose a `PlaceRoom` entry with
        this node's region as home and drive it to commit.

        * Not the leader? Forward to it, or raise `NotLeaderError(leader)` so
          signaling can — your call, and a line in the design doc.
        * Cannot reach `config.quorum`? Raise `UnavailableError`. Refusing is the
          whole safety property: a minority that "just picks a home for now" is how
          a split room is born, and it is born silently.
        * Enforce `config.max_rooms` **at commit/apply time** (raise
          `ConflictError` to the proposer). A per-node counter checked before
          proposing lets two nodes each admit the last slot.
        * Bump `ROOMS_PLACED` / `PLACEMENT_COMMITS_TOTAL` when the entry applies.
        """
        raise NotImplementedError(
            "V1: place the room via consensus (idempotent, one home, minority refuses)"
        )

    async def register_interest(self, room_id: str, region: str, *, joined: bool) -> None:
        """Replicate that `region` gained its first / lost its last participant in `room_id`.

        TODO(V1): commit a `RegionInterest` entry so every node's `active_regions`
        for the room converges, then apply it (new `RoomPlacement`, epoch + 1).
        Idempotent in the same way placement is: a region joining a room it is
        already active in must not bump the epoch. Leaving drops the region from
        the set — and that change is what lets V2 tear a relay leg down.

        Note the signature takes a *region*, not a participant. Counting local
        participants so that only the first join and the last leave reach the log
        is a local concern; replicating every join would put the join storm in the
        boss fight straight onto the consensus path.
        """
        raise NotImplementedError("V1: replicate the membership change, apply on commit")

    async def on_vote(self, request: VoteRequest) -> VoteReply:
        """Answer a peer's `POST /cluster/vote`.

        TODO(V1): adopt a higher term (and step down to follower if you were
        anything else), grant at most one vote per term, and deny a candidate
        whose log is behind yours — which needs the fields `rpc.py` tells you to
        add. Reply with your current term either way. Update `NODE_TERM` /
        `NODE_ROLE`.
        """
        raise NotImplementedError(
            "V1: RequestVote — grant at most one vote per term, adopt higher terms"
        )

    async def on_replicate(self, request: ReplicateRequest) -> ReplicateReply:
        """Answer the leader's `POST /cluster/replicate` (an empty `entries` is a heartbeat).

        TODO(V1): reject a stale term; otherwise record the leader, reset the
        election timer, append the entries (after the consistency check your
        extended request enables), and apply whatever is newly committed to
        `self._rooms` in log order. Apply must be idempotent — a retried replicate
        delivers the same entries twice, and applying a `PlaceRoom` twice must not
        change a committed home.
        """
        raise NotImplementedError(
            "V1: AppendEntries — append/commit entries, apply to the map, reset election timer"
        )

    async def run(self) -> None:
        """The election + heartbeat loop. Runs until cancelled.

        TODO(V1): as a follower, wait a random interval in
        `[election_timeout, 2 * election_timeout)` (`random.uniform`) for a
        heartbeat; if none arrives, become candidate, bump the term, and canvass
        `self._cluster.regions` concurrently (see `cluster.py` on why concurrently,
        and on why `PeerUnreachableError` is an ordinary non-vote). As leader, send
        `replicate` to every peer every `config.heartbeat`. Bump `ELECTIONS_TOTAL`
        per election started.

        Cancellation is the shutdown signal — `main` cancels this task on SIGTERM.
        Do not swallow `asyncio.CancelledError` in a broad `except`: catch
        `PeerUnreachableError`, not `Exception`, or the loop will outlive shutdown.

        Gated behind `RUN_BACKGROUND` so the bare scaffold boots without driving an
        election with no peers.
        """
        raise NotImplementedError(
            "V1: run the election/heartbeat loop (randomized timeout, single leader per term)"
        )

    async def step_down(self) -> None:
        """Relinquish leadership on graceful shutdown, so the mesh re-elects fast.

        TODO(V1): a no-op unless this node leads. A leader that simply vanishes
        costs the mesh a full election timeout with no leader — and every
        `publish` for a new room in that window refuses. How you shorten that
        window (and what a follower should make of it) is a design-doc line.
        Called from the lifespan's shutdown, before the cluster client closes.
        """
        raise NotImplementedError("V1: relinquish leadership if held (graceful shutdown)")
