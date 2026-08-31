"""V4 — Snapshots & log compaction: keeping the log from growing forever.

An append-only log grows without bound, and replaying it from index 1 on every
restart gets slower forever. The fix: periodically **snapshot** the state machine
(the whole KV map is far smaller than the history that produced it), then
**discard** every log entry the snapshot covers. The snapshot records the
`last_included_index` / `last_included_term` it replaces, so the log's consistency
checks still line up at that boundary.

This also changes replication. A leader that has compacted past what a slow
follower needs can no longer send those entries — they're gone. So it sends the
**whole snapshot** via `InstallSnapshot`; the follower adopts it wholesale and
resumes from `last_included_index + 1`.

The trap: compaction races with everything. You may only discard entries at or
below `last_applied` (never un-applied or un-committed ones), and the snapshot
must be durable **before** the log is truncated — or a crash mid-compaction loses
committed state that now exists in neither place.

## What asyncio adds to that trap

The snapshot is the one operation here that is genuinely *big*: serializing the
whole map and writing it to disk. Both halves are blocking, and both must go
through `asyncio.to_thread` or the node stops answering heartbeats for the
duration and gets voted out mid-compaction. But moving the work off the loop
introduces the second half of the problem: `store.snapshot()` must be called for a
consistent instant. Serialize the map *synchronously* to get the bytes and the
`last_applied` they correspond to, then hand those already-frozen bytes to the
thread for writing. Serializing inside the thread means iterating a dict that the
loop is still mutating — which in CPython raises `RuntimeError: dictionary changed
size during iteration` if you are lucky, and produces a snapshot of a state that
never existed if you are not.

And the ordering is not just "durable before truncate" but "durable before
truncate, with no `await` in between that could let a *second* compaction start".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .rpc import InstallSnapshotArgs, InstallSnapshotReply, NodeId

if TYPE_CHECKING:  # import cycle: node imports this module at runtime
    from .node import RaftNode

__all__ = ["handle_install_snapshot", "maybe_snapshot", "send_snapshot"]


async def maybe_snapshot(node: RaftNode) -> None:
    """Take a snapshot and compact the log if it has grown past the threshold (V4).

    Called from the apply path after `last_applied` advances.

    TODO(V4): if `len(node.log) > node.config.snapshot_threshold` —
      * capture the boundary `(last_applied, node.log.term_at(last_applied))` and
        the serialized machine (`node.store.snapshot()`) as one synchronous,
        `await`-free step, so the bytes and the index they claim to represent
        cannot disagree;
      * write the snapshot durably (in a thread — see the module docstring);
      * **only then** `node.log.compact_to(last_index, last_term)` and persist the
        shortened log.

    Clamp the boundary to `last_applied`, never `commit_index` and never
    `log.last_index`. Compacting past what has been applied throws away entries
    the state machine has not folded in yet — the snapshot would then be missing
    their effects *and* the log would be missing the entries, which is
    unrecoverable rather than merely wrong.
    """
    raise NotImplementedError("V4: snapshot the state machine and compact past last_applied")


async def handle_install_snapshot(
    node: RaftNode, args: InstallSnapshotArgs
) -> InstallSnapshotReply:
    """Handle an inbound `InstallSnapshot` (V4) — a leader is handing us a snapshot
    because the entries we need have been compacted away on its side.

    TODO(V4):
      * reject if `args.term < node.log.current_term` (reply our term and change
        nothing);
      * otherwise adopt the term, step down, and `node.reset_election_timer()` —
        a snapshot transfer can take a while, and a follower that starts an
        election halfway through gets nowhere and has to start over;
      * if the snapshot is genuinely newer than our state,
        `node.store.restore(args.data)`, then
        `node.log.reset_to_snapshot(args.last_included_index, args.last_included_term)`
        and set `commit_index` and the store's applied point to that index;
      * persist, then reply with our current term.

    "If it is newer" is a real guard, not a formality: a delayed or duplicated
    `InstallSnapshot` carrying an *older* boundary must be ignored, or it will roll
    this node's state machine backwards past entries it has already applied and
    acknowledged.

    Note `args.data` arrives as plain `bytes` — the base64 on the wire is decoded
    for you by the model (see `rpc.py`). Treat those bytes as untrusted input from
    the network: whatever `store.restore` does with them is the peer-trust
    boundary the security checklist asks you to state.
    """
    raise NotImplementedError("V4: install the leader's snapshot, replacing state + log base")


async def send_snapshot(node: RaftNode, peer: NodeId) -> None:
    """Ship the current snapshot to a lagging `peer` whose `next_index` has fallen
    to or below the snapshot boundary (V4).

    Called from replication when plain `AppendEntries` can no longer reach that
    far back.

    TODO(V4): build `InstallSnapshotArgs` from the persisted snapshot, send it
    with `node.peer_client.install_snapshot`, and on success set that peer's
    `next_index` to `last_included_index + 1` and `match_index` to
    `last_included_index`.

    Watch the size. This one message can be orders of magnitude larger than a
    heartbeat, and the peer client's timeout is tuned for heartbeats — a snapshot
    that cannot transfer inside that deadline will retry forever, consuming
    bandwidth and never catching the follower up. Either give this call its own
    longer deadline or chunk the transfer (the stretch goal), and either way don't
    let it block the heartbeats to the *other* followers while it runs.
    """
    raise NotImplementedError("V4: send the snapshot to a follower behind the compacted log")
