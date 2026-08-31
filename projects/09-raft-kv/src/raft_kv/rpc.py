"""Shared vocabulary — the ids, the log entry, and the four wire messages every
node speaks to every other node.

Plumbing, not a vertical: these are plain, serializable data. They're grouped
here (the "record.py" of this project) so `log.py` can stay purely about
*storing* entries and the consensus modules can stay about *deciding* things.

Two RPCs carry the whole protocol — `RequestVote` (elections, V1) and
`AppendEntries` (replication *and* heartbeats, V2) — plus `InstallSnapshot`
(V4) for a follower that has fallen behind the leader's compacted log.

Three Python decisions worth understanding before you build on them:

**1. The ids are transparent aliases, not new types.** `Term = int` gives the
signatures a vocabulary without inventing a runtime type: a `Term` *is* an `int`
everywhere. `typing.NewType` would make the checker treat them as distinct (so
passing a `LogIndex` where a `Term` belongs would be an error) at the price of
wrapping every literal in `Term(0)`. That trade is worth revisiting once the
index math in `log.py` has bitten you; it is not worth paying up front.

**2. `Command` is a discriminated union, not an enum.** Rust's `enum Command`
carried per-variant data, which Python's `enum.Enum` cannot. The equivalent is a
union of small models tagged by a literal `op` field, which gives you the same
two things the Rust enum did: JSON that names its own variant, and exhaustive
`match` dispatch in `store.apply`. Pydantic's `discriminator="op"` makes the
decode a dict lookup on `op` rather than "try each member until one validates" —
so a malformed command is a clear error about `op`, not three stacked ones.

**3. The snapshot blob rides as base64, not as a list of integers.** `data:
bytes` with `ser_json_bytes="base64"` / `val_json_bytes="base64"` keeps the
Python side plain `bytes` while the JSON side is a compact base64 string. This
is a real improvement on the Rust scaffold: serde's default for `Vec<u8>` is a
JSON *array of numbers*, which costs roughly 4x the bytes for the one message
that is already the largest thing the cluster sends.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AppendEntriesArgs",
    "AppendEntriesReply",
    "ClientResponse",
    "Command",
    "DeleteCommand",
    "InstallSnapshotArgs",
    "InstallSnapshotReply",
    "LogEntry",
    "LogIndex",
    "NodeId",
    "NoopCommand",
    "RequestVoteArgs",
    "RequestVoteReply",
    "SetCommand",
    "Term",
]

type NodeId = int
"""A node's identity in the cluster. Small, stable, assigned by config."""

type Term = int
"""A Raft *term* — a logical clock that only ever increases. Every message
carries one; the higher term always wins, and seeing a higher term forces a node
back to follower. This single rule is what makes the protocol safe."""

type LogIndex = int
"""A 1-based position in the replicated log. Index 0 means "empty log"."""


# ---- Commands (what the state machine applies) ------------------------------


class SetCommand(BaseModel):
    """Write a value. The `op` field is the discriminator, not decoration."""

    op: Literal["set"] = "set"
    key: str
    value: str


class DeleteCommand(BaseModel):
    op: Literal["delete"] = "delete"
    key: str


class NoopCommand(BaseModel):
    """The entry a fresh leader appends on election.

    It carries nothing, and that is the point: it exists so the leader has an
    entry *in its own term* to replicate, which is the only kind it is allowed to
    commit by replica count (V2, Raft §5.4.2).
    """

    op: Literal["noop"] = "noop"


type Command = Annotated[SetCommand | DeleteCommand | NoopCommand, Field(discriminator="op")]
"""A command the state machine (V3) can apply — what a log entry carries and what
a client write turns into."""


class LogEntry(BaseModel):
    """One entry in the replicated log: a command stamped with the term it was
    created in and its index.

    The `(term, index)` pair is the heart of the Log Matching property (V2) — if
    two logs hold an entry with the same term at the same index, they agree on
    everything before it.
    """

    term: Term
    index: LogIndex
    command: Command


# ---- RequestVote (V1 — elections) ------------------------------------------


class RequestVoteArgs(BaseModel):
    """Sent by a candidate to gather votes.

    A peer grants its vote at most once per term, and only to a candidate whose
    log is *at least as up-to-date* as its own — which is what `last_log_index`
    and `last_log_term` are here to let it judge.
    """

    term: Term
    candidate_id: NodeId
    last_log_index: LogIndex
    last_log_term: Term


class RequestVoteReply(BaseModel):
    term: Term
    """The voter's current term, so a stale candidate learns it has been left
    behind."""

    vote_granted: bool


# ---- AppendEntries (V2 — replication + heartbeat) ---------------------------


class AppendEntriesArgs(BaseModel):
    """Sent by the leader to replicate entries — and, with an empty `entries`, as
    the heartbeat that suppresses new elections.

    `prev_log_*` is the consistency check: the follower only accepts if it has
    that exact entry, which is how a diverged follower's log gets walked back and
    repaired.
    """

    term: Term
    leader_id: NodeId
    prev_log_index: LogIndex
    prev_log_term: Term
    entries: list[LogEntry] = []
    leader_commit: LogIndex
    """The leader's commit index, so followers learn what's safe to apply."""


class AppendEntriesReply(BaseModel):
    term: Term
    success: bool
    conflict_index: LogIndex | None = None
    """Optional fast-backup hint: the index the leader should retry from when the
    consistency check fails, so it doesn't decrement `next_index` one at a time.
    Its exact meaning is yours to define — and to write down (a horizontal item)."""


# ---- InstallSnapshot (V4 — catching up a lagging follower) ------------------


class InstallSnapshotArgs(BaseModel):
    """Sent when the entries a follower needs have already been compacted away
    into a snapshot.

    The follower adopts the snapshot wholesale and discards its log up to
    `last_included_index`.
    """

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    term: Term
    leader_id: NodeId
    last_included_index: LogIndex
    last_included_term: Term
    data: bytes
    """The serialized state-machine snapshot, base64 on the wire. Sent whole
    here; chunking it is a stretch goal — and the reason to chunk is that this
    one message can be orders of magnitude larger than a heartbeat."""


class InstallSnapshotReply(BaseModel):
    term: Term


class ClientResponse(BaseModel):
    """What a committed client command returns once it has been applied (V3): the
    prior value for a `Set`/`Delete`, if any."""

    value: str | None = None
