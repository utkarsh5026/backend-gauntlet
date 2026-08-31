"""V2 — Log replication: keeping every node's log identical, and knowing when an
entry is safe to apply.

Once elected, the leader is the only writer. A client command becomes a log entry
on the leader, which then pushes it to followers with `AppendEntries`. The magic
is the **consistency check**: every `AppendEntries` names the
`(prev_log_index, prev_log_term)` the new entries must follow. A follower accepts
only if it holds that exact entry; otherwise it rejects, the leader walks
`next_index` back, and retries — repairing a diverged tail. This yields the **Log
Matching** property: same index + same term => identical histories up to there.

**Commit** is the other half. An entry is committed once it's on a **majority** —
at which point the leader advances `commit_index`, and every node applies up to
it, in order, to the state machine (V3). The subtle safety rule: a leader may only
advance `commit_index` to an entry **from its own term**. Counting replicas of a
*previous* term's entry and committing it can lose data (Raft §5.4.2) — the reason
a new leader appends that no-op first.

Guarantee delivered: **at-least-once apply of committed commands, in a total order
identical on every node.** (Exactly-once at the *client* needs request dedup on
top — the V3 stretch.)

## The two Python problems in this file

**Waiting for a commit.** `propose` has to block a client request until some
future moment when the apply loop reaches that index. Do not poll for it. The
idiomatic tool is a **future per waiting index**: `propose` creates an
`asyncio.Future`, parks it in a `dict[LogIndex, Future[...]]` on the node, and
awaits it; `apply_committed` looks the index up as it applies and calls
`set_result`. That gives you a wakeup with no latency and no busy loop, and — the
part that actually matters — it gives you a place to `set_exception(NotLeader())`
on every parked waiter when this node steps down. A client whose write was
proposed by a leader that then lost the election must get an error, not a hang.
Wrap the await in `asyncio.timeout` too: "committed eventually" is not a promise
you can make to an HTTP client holding a connection open.

**Applying is a loop, and it must not run twice.** `apply_committed` can be
called from the heartbeat path and the client path at once. Two concurrent
callers both reading `last_applied`, both deciding to apply index N+1, is exactly
the double-apply that `store.apply`'s gap check exists to catch — and catching it
at that point is too late to be graceful. Either serialize this function (the one
clearly justified use of `node.lock`) or make it a single long-lived consumer task
the others merely nudge. Pick one and write down which, because it is the
"one clear ownership model" checklist item.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .rpc import AppendEntriesArgs, AppendEntriesReply, ClientResponse, Command, NodeId

if TYPE_CHECKING:  # import cycle: node imports this module at runtime
    from .node import RaftNode

__all__ = [
    "apply_committed",
    "broadcast_append_entries",
    "handle_append_entries",
    "maybe_advance_commit",
    "peer_progress",
    "propose",
    "read",
]


async def handle_append_entries(node: RaftNode, args: AppendEntriesArgs) -> AppendEntriesReply:
    """Handle an inbound `AppendEntries` (V2) — the follower side of replication,
    and also the heartbeat that resets the election timer.

    TODO(V2): the follower's accept/reject logic —
      * reply `success=False` if `args.term < node.log.current_term` (a stale
        leader that hasn't noticed it was deposed);
      * if `args.term >= node.log.current_term` -> adopt the term,
        `node.become_follower(args.term, args.leader_id)`, and
        `node.reset_election_timer()`. **This is the heartbeat.** Note the `>=`:
        an equal term from the current leader still resets the timer, and missing
        that is how a healthy cluster starts electing anyway;
      * consistency check: reply `False` unless we hold an entry at
        `args.prev_log_index` whose term is `args.prev_log_term`
        (`node.log.term_at` answers this, including at the snapshot seam).
        Optionally return a `conflict_index` hint so the leader backs up in one
        jump rather than one index per round trip;
      * on match: delete any conflicting suffix (`log.truncate_from`), append
        `args.entries`, then advance `node.commit_index` to
        `min(args.leader_commit, index of the last new entry)`. That `min` is
        load-bearing — a follower must never claim to have committed past what it
        actually holds;
      * `await node.log.persist()` before replying `True`. The reply is a promise
        that the entry is durable here; make it true before you make it.

    One trap worth naming: truncate *only* on an actual conflict. A retried or
    reordered `AppendEntries` carrying entries this node already has must be a
    no-op, not a truncate-and-reappend — blindly truncating on every request can
    chop off entries the leader has already counted toward a commit.
    """
    raise NotImplementedError("V2: consistency check, repair the tail, advance commit_index")


async def broadcast_append_entries(node: RaftNode) -> None:
    """Replicate to (or heartbeat) every peer once (V2).

    Called by the driver's heartbeat ticker while this node is leader.

    TODO(V2): for each peer, build an `AppendEntries` from `next_index[peer]`
    (the entries from there on, with the matching `prev_log_*`), send them all
    concurrently, and react per peer:
      * success -> advance `match_index`/`next_index` for that peer, then try
        `maybe_advance_commit(node)`;
      * failure carrying a higher term -> `node.become_follower(reply.term)` and
        abandon the round;
      * failure on the consistency check -> decrement `next_index` (or jump to the
        `conflict_index` hint) and let the next round retry. Do not loop here
        until it succeeds: that turns one slow follower into a stalled heartbeat
        for everybody;
      * if `next_index[peer]` has fallen at or below the snapshot base, the
        entries are *gone* — send `snapshot.send_snapshot(node, peer)` instead (V4);
      * an exception is a peer that didn't answer this round. Log it at debug and
        move on.

    Compute `next_index` and slice the entries *before* the await, then re-check
    you are still leader in the same term before applying the results — the
    election could have moved on while these RPCs were in flight.
    """
    raise NotImplementedError("V2: send AppendEntries to each peer and process the replies")


def maybe_advance_commit(node: RaftNode) -> None:
    """After replication progress, advance the leader's commit index if a majority
    now holds an entry **from the current term** (V2's safety rule).

    TODO(V2): find the highest index `n` such that
      * `match_index >= n` on a `node.quorum` of nodes, counting this leader
        itself (it trivially has everything in its own log — forgetting to count
        self is why a 3-node cluster appears to need all 3 to commit);
      * `node.log.term_at(n) == node.log.current_term`;
      * `n > node.commit_index`;
    then set `node.commit_index = n` and let the apply path catch up.

    Sorting the match indices descending and taking the `quorum - 1`-th element is
    the neat way to find the highest majority-replicated index; the current-term
    filter is applied *after*, and skipping it is precisely the §5.4.2 data-loss
    bug the SPEC asks you to construct a regression test for.

    Synchronous on purpose: it reads and writes consensus state with no `await`
    anywhere, so it is atomic against the loop and needs no lock.
    """
    raise NotImplementedError("V2: advance commit_index to the highest quorum-replicated entry")


async def apply_committed(node: RaftNode) -> None:
    """Apply every newly-committed entry to the state machine, in order (V2 -> V3).

    Called whenever `commit_index` moves, on leader and followers alike.

    TODO(V2/V3): while `store.last_applied < node.commit_index`, fetch the entry
    at `last_applied + 1`, `node.store.apply(...)` it, and hand the result to any
    client parked on that index. This is the single point where the log becomes
    state — and the reason every node ends up with the same map is that every node
    runs exactly this loop over exactly the same entries.

    Read the module docstring on why this function must not run concurrently with
    itself, and on the waiter futures. Also: if the entry at `last_applied + 1` is
    missing because it was compacted away, that is not a gap to skip — it means
    this node's state machine is behind its own snapshot, which should be
    impossible, and it deserves a loud failure rather than a silent one.
    """
    raise NotImplementedError("V2/V3: apply committed entries to the Store in index order")


async def propose(node: RaftNode, command: Command) -> ClientResponse:
    """The client write path: propose a command, return once it is committed and
    applied (V2 -> V3). Called by `PUT /kv/{key}` and `DELETE /kv/{key}`.

    TODO(V2):
      * if this node is not the leader, `raise node.not_leader()` — the client gets
        a redirect to whoever we think is, which is strictly more useful than a
        bare error. Never serve the write locally "just this once";
      * otherwise append the command to the local log in the current term,
        `await node.log.persist()`, kick replication, and **wait** until that index
        is committed and applied before returning its result;
      * a leadership change while waiting must surface as `NotLeader`, not a hang
        — see the future-per-index note in the module docstring.

    Note what this signature promises: it returns *after* the command is applied,
    so the value it reports is the state machine's own answer, not a prediction.
    That is what lets the caller read its own write back immediately.
    """
    raise NotImplementedError("V2: append, replicate to a quorum, wait for apply")


async def read(node: RaftNode, key: str) -> str | None:
    """The linearizable read path (V3's read side, enforced here). Called by
    `GET /kv/{key}`.

    TODO(V2/V3): a correct read must not serve stale data.
      * reject with `node.not_leader()` if not leader;
      * then confirm this node still leads **now** — a leader partitioned away a
        millisecond ago still believes it is leader, and its map is already stale.
        The read-index technique: note the current `commit_index`, exchange a
        successful heartbeat round with a quorum (proving no one has elected
        anyone else), and only then serve;
      * and confirm the state machine has applied through that read index, or the
        value is from before a write that already completed;
      * only then `node.store.get(key)`.

    The alternative is a **leader lease**: serve locally without the round trip,
    on the assumption that no election can complete within one election timeout of
    the last successful heartbeat. Faster, and correct only if clocks behave.
    Whichever you choose, `docs/09-design.md` has to name it *and* its assumption
    — that is the graded part, because both are defensible and only one of them is
    the one you built.
    """
    raise NotImplementedError("V2/V3: confirm leadership (read-index), then serve applied state")


def peer_progress(node: RaftNode, peer: NodeId) -> tuple[int, int]:
    """`(next_index, match_index)` for `peer`, or `(last_index + 1, 0)` if this
    node has no leader state.

    A small wired convenience so the V2 code above doesn't repeat the
    "am I leader / is this peer known" dance at every call site.
    """
    leader_state = node.leader_state
    if leader_state is None:
        return node.log.last_index + 1, 0
    return (
        leader_state.next_index.get(peer, node.log.last_index + 1),
        leader_state.match_index.get(peer, 0),
    )
