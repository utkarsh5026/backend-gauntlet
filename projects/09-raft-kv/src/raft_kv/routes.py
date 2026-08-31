"""HTTP surface — two audiences on one server.

**Clients** talk to the KV API (`/kv/*`, `/status`). Writes and linearizable reads
only succeed on the leader; a follower answers with a redirect to the leader it
knows (`errors.NotLeader`).

**Peers** talk to the `/raft/*` RPC endpoints — the receive side of the two
consensus RPCs (and `InstallSnapshot`). These deserialize the args, hand them to
the vertical modules (V1/V2/V4), and serialize the reply. The routing and shapes
are wired; what the handlers call into is where the `NotImplementedError`s live.

Scaffold behavior: `GET /healthz`, `GET /status` and `GET /metrics` work
immediately — `/status` in particular is how you watch an election happen once V1
exists. A client write, a linearizable read, or any inbound RPC raises, and that
message is the worklist.

**One thing the router does not do yet, on purpose.** The `/raft/*` endpoints let
any caller drive consensus: force a term bump, inject entries, install a snapshot.
They are an unauthenticated remote control for the cluster's state. The SPEC grades
this as a horizontal item — state the trust boundary (private network? mTLS between
nodes?) and put a credential on the client API. Both are yours to add; the
`TODO(security)` markers below are where.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import election, replication, snapshot
from .errors import InvalidRequest, KeyNotFound
from .rpc import (
    AppendEntriesArgs,
    AppendEntriesReply,
    DeleteCommand,
    InstallSnapshotArgs,
    InstallSnapshotReply,
    RequestVoteArgs,
    RequestVoteReply,
    SetCommand,
)
from .state import AppState, get_state

__all__ = ["MAX_KEY_BYTES", "MAX_VALUE_BYTES", "router"]

MAX_KEY_BYTES = 1024
"""A key is replicated to every node, held in every state machine, and lives in
every snapshot forever. It has no business being large."""

MAX_VALUE_BYTES = 256 * 1024
"""Every write is copied to every node and re-sent in full on every snapshot
transfer, so an unbounded value is an unbounded cost multiplied by the cluster
size. The cap is the "size validation on client input" security item."""

StateDep = Annotated[AppState, Depends(get_state)]

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only.

    Deliberately *not* a readiness check: this answers `ok` on a follower, on a
    candidate, and during an election. A node that is up but leaderless is
    healthy — it is doing exactly what Raft says to do — and wiring an
    orchestrator to restart it would turn a normal election into a crash loop.
    """
    return {"status": "ok"}


@router.get("/status")
async def status(state: StateDep) -> dict[str, Any]:
    """Role, term, leader, and commit/apply progress.

    Wired and safe to call in any state — this is how you watch an election
    happen, and the first place to look when the cluster is doing something you
    didn't expect.
    """
    return state.node.status()


# ---- Client KV API ----------------------------------------------------------


class PutBody(BaseModel):
    value: str = Field(max_length=MAX_VALUE_BYTES)


class WriteResponse(BaseModel):
    key: str
    previous: str | None = None
    """What the state machine held before this command applied. It comes back from
    the apply step, not from a read before the write — so it is the real prior
    value at that log index, not a guess that a concurrent write could invalidate."""


class ReadResponse(BaseModel):
    key: str
    value: str


def _validate_key(key: str) -> str:
    """Reject keys that are empty or oversized before they enter the log.

    Before, specifically, because a log entry is *permanent*: a bad key that gets
    replicated and committed is now in every node's log and every future snapshot.
    Validation at the edge is the only place it is cheap.
    """
    if not key:
        raise InvalidRequest("empty key")
    if len(key.encode()) > MAX_KEY_BYTES:
        raise InvalidRequest(f"key must be <= {MAX_KEY_BYTES} bytes")
    return key


@router.put("/kv/{key}")
async def put_key(key: str, body: PutBody, state: StateDep) -> WriteResponse:
    """A write. Goes through Raft: appended on the leader, replicated to a quorum,
    applied, then answered (V2 -> V3).

    TODO(security): require a credential here. An open KV endpoint is an open
    datastore, and this one replicates whatever it is given to every node.
    """
    _validate_key(key)
    response = await replication.propose(state.node, SetCommand(key=key, value=body.value))
    return WriteResponse(key=key, previous=response.value)


@router.get("/kv/{key}")
async def get_key(key: str, state: StateDep) -> ReadResponse:
    """A **linearizable** read — served only by a leader that has confirmed it
    still leads (V2/V3 read path). `404` if the key is unset.

    The 404 is the state machine's answer, not a shortcut: it is returned only
    after the read path has established that this node is entitled to answer at
    all. "I am not sure who the leader is, so probably no such key" is exactly the
    stale read the SPEC forbids.
    """
    _validate_key(key)
    value = await replication.read(state.node, key)
    if value is None:
        raise KeyNotFound()
    return ReadResponse(key=key, value=value)


@router.delete("/kv/{key}")
async def delete_key(key: str, state: StateDep) -> WriteResponse:
    """A delete — also a replicated command, for the same reason a write is.

    A delete that only happened locally would leave the cluster's nodes
    disagreeing about whether a key exists, which is the same failure as a lost
    write wearing a different hat.
    """
    _validate_key(key)
    response = await replication.propose(state.node, DeleteCommand(key=key))
    return WriteResponse(key=key, previous=response.value)


# ---- Peer RPC (V1 / V2 / V4) ------------------------------------------------
#
# TODO(security): these three are the cluster's remote control — see the module
# docstring. At minimum, state the trust boundary in `docs/09-design.md`.


@router.post("/raft/request-vote")
async def request_vote(args: RequestVoteArgs, state: StateDep) -> RequestVoteReply:
    """A candidate is asking for our vote (V1)."""
    return await election.handle_request_vote(state.node, args)


@router.post("/raft/append-entries")
async def append_entries(args: AppendEntriesArgs, state: StateDep) -> AppendEntriesReply:
    """The leader is replicating (or heartbeating) (V2)."""
    return await replication.handle_append_entries(state.node, args)


@router.post("/raft/install-snapshot")
async def install_snapshot(args: InstallSnapshotArgs, state: StateDep) -> InstallSnapshotReply:
    """The leader is shipping us a snapshot (V4)."""
    return await snapshot.handle_install_snapshot(state.node, args)
