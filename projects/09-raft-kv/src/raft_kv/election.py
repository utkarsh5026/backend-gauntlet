"""V1 — Leader election: turning N equal peers into one leader, safely.

Raft's answer to "who's in charge?" is a randomized timeout race. A follower that
hasn't heard from a leader within its (random) election timeout becomes a
**candidate**: it increments the term, votes for itself, and asks every peer for a
vote. A peer grants at most **one vote per term**, and only to a candidate whose
log is *at least as up-to-date* as its own. Win a majority -> leader. Split the
vote -> nobody wins, everyone times out again at a *new* random interval, and the
split is unlikely to repeat.

The two rules that make it *safe* (not just live), and the whole point of V1:

  1. **One vote per term, and remember it across a crash.** A node that forgets it
     voted can vote twice and elect two leaders. Hence `await log.persist()`
     before replying to a vote — before, not after, and not concurrently.
  2. **Up-to-date check.** A candidate missing committed entries must *lose*.
     Compare `(last_log_term, last_log_index)` as a pair, in that order: a voter
     refuses anyone whose log ends on an older term, or on the same term but
     shorter. Python compares tuples exactly this way, so
     `(their_term, their_index) >= (my_term, my_index)` is the whole rule and
     reads like the spec sentence. This is what stops a stale node from erasing
     committed history.

**The asyncio hazard specific to this file.** `start_election` awaits peer RPCs in
the middle. While it waits, this node's own `handle_request_vote` and
`handle_append_entries` can run and change `role` and `current_term` under it. So
the votes coming back may be answers to an election this node has already lost or
abandoned. Snapshot the term you are campaigning for into a local before the
await, and when the replies land, check that `node.role is Role.CANDIDATE` and
`node.log.current_term` still equals it before declaring victory. Counting a
majority is not enough; counting a majority *for the term you are still in* is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .rpc import RequestVoteArgs, RequestVoteReply

if TYPE_CHECKING:  # import cycle: node imports this module at runtime
    from .node import RaftNode

__all__ = ["become_leader", "handle_request_vote", "start_election"]


async def handle_request_vote(node: RaftNode, args: RequestVoteArgs) -> RequestVoteReply:
    """Handle an inbound `RequestVote` (V1) — the voter's side.

    Called by the `/raft/request-vote` endpoint when some candidate is canvassing.

    TODO(V1): implement the voter's decision —
      * `args.term < node.log.current_term` -> reject, replying our term so the
        stale candidate learns it has been left behind;
      * `args.term > node.log.current_term` -> adopt it and step down
        (`node.become_follower(args.term)`), *then* judge the request on its
        merits — adopting a higher term does not by itself grant a vote;
      * grant iff we have not voted this term (or already voted for this same
        candidate — a retried RPC must get the same answer, since Raft's transport
        is at-least-once) **and** the candidate's log is at least as up-to-date as
        ours;
      * on granting: record `voted_for`, `node.reset_election_timer()`, and
        `await node.log.persist()` **before** returning.

    That last ordering is the one to be pedantic about. Returning the reply first
    and persisting after leaves a window where a crash loses the vote — and the
    rebooted node will happily grant a second one in the same term. Nothing in a
    test will catch it; only a crash at the wrong microsecond will.
    """
    raise NotImplementedError("V1: decide this vote, persisting it before replying")


async def start_election(node: RaftNode) -> None:
    """Become a candidate and run an election (V1).

    Called by the driver when the election timer fires.

    TODO(V1): the candidate side —
      * transition to `Role.CANDIDATE`, `current_term += 1`, vote for self,
        `await node.log.persist()`;
      * capture `(term, last_log_index, last_log_term)` into locals, then canvass
        every peer **concurrently** — `asyncio.gather(..., return_exceptions=True)`
        over `node.peer_client.request_vote`. Sequential awaits make an election
        take longer than the timeout that started it (see `peer.py`);
      * count grants, including our own self-vote, and on reaching `node.quorum`
        *while still a candidate in the same term* -> `become_leader(node)`;
      * if any reply carries a higher term -> `node.become_follower(reply.term)`
        and stop; a candidate that ignores a higher term is the bug that elects
        two leaders;
      * a peer that raised (down, partitioned, timed out) is simply a missing vote
        — `return_exceptions=True` hands you the exception object instead of
        cancelling the round, so filter those out and keep going.
    """
    raise NotImplementedError("V1: run an election — bump term, self-vote, canvass, count a quorum")


async def become_leader(node: RaftNode) -> None:
    """Transition a freshly-won candidate to leader (the V1 -> V2 seam).

    TODO(V1/V2): on becoming leader —
      * set `role = Role.LEADER`, `leader_id = node.id`, and install a fresh
        `LeaderState` with `next_index[peer] = log.last_index + 1` and
        `match_index[peer] = 0` for every peer. Optimistic `next_index` and
        pessimistic `match_index` is not an accident: the leader assumes followers
        match until the consistency check proves otherwise, and assumes nothing is
        replicated until a follower confirms it;
      * append a `NoopCommand` entry in the new term and start replicating it.
        This is the subtle one. Without an entry of its own term, a fresh leader
        cannot commit *anything* — not even old entries a majority already holds —
        because of the current-term commit rule (V2, Raft §5.4.2). The no-op is
        what unblocks the log, and it is why a leader change costs one extra round
        trip before the first write can commit;
      * start the heartbeat task so followers stop timing out.
    """
    raise NotImplementedError("V1/V2: initialize leader state, append a no-op, begin heartbeating")
