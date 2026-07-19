//! V1 — Leader election: turning N equal peers into one leader, safely.
//!
//! Raft's answer to "who's in charge?" is a randomized timeout race. A follower
//! that hasn't heard from a leader within its (random) election timeout becomes a
//! **candidate**: it increments the term, votes for itself, and asks every peer
//! for a vote. A peer grants at most **one vote per term**, and only to a
//! candidate whose log is *at least as up-to-date* as its own. Win a majority →
//! leader. Split the vote → nobody wins, everyone times out again at a *new*
//! random interval, and the split is unlikely to repeat.
//!
//! The two rules that make it *safe* (not just live), and the whole point of V1:
//!   1. **One vote per term, and remember it across a crash.** A node that forgets
//!      it voted can vote twice and elect two leaders. Hence `persist()` before
//!      replying to a vote.
//!   2. **Up-to-date check.** A candidate missing committed entries must *lose*.
//!      Compare `(last_log_term, last_log_index)` lexicographically: a voter
//!      refuses anyone whose log ends on an older term, or same term but shorter.
//!      This is what stops a stale node from erasing committed history.

use std::sync::Arc;

use crate::node::RaftNode;
use crate::rpc::{RequestVoteArgs, RequestVoteReply};

impl RaftNode {
    /// Handle an inbound `RequestVote` (V1). Called by the `/raft/request-vote`
    /// endpoint when some candidate is canvassing.
    ///
    /// TODO(V1): implement the voter's decision —
    ///   - if `args.term` < our term → reject (reply our term, `vote_granted=false`);
    ///   - if `args.term` > our term → adopt it and step down (`become_follower`);
    ///   - grant the vote iff we haven't voted this term (or already voted for this
    ///     candidate) **and** the candidate's log is at least as up-to-date as ours
    ///     (the `(last_log_term, last_log_index)` comparison);
    ///   - on granting: record `voted_for`, reset the election timer, and
    ///     `persist()` **before** replying.
    pub async fn handle_request_vote(&self, args: RequestVoteArgs) -> RequestVoteReply {
        let _ = (&self.inner, &args);
        todo!("V1: decide whether to grant this vote, persisting the vote before replying")
    }

    /// Become a candidate and run an election (V1). Called by the driver when the
    /// election timer fires.
    ///
    /// TODO(V1): the candidate side —
    ///   - transition to Candidate, `current_term += 1`, vote for self, `persist()`;
    ///   - snapshot `(term, last_log_index, last_log_term)`, then **drop the lock**
    ///     and `request_vote` every peer concurrently;
    ///   - count grants (including our self-vote); on reaching `quorum()` *and*
    ///     still being a candidate in the same term → `become_leader`;
    ///   - if any reply carries a higher term → step down;
    ///   - a peer that errors/times out is simply a missing vote — keep going.
    pub async fn start_election(self: &Arc<Self>) {
        let _ = &self.inner;
        todo!("V1: run an election — bump term, self-vote, canvass peers, count a quorum")
    }

    /// Transition a freshly-won candidate to leader (V1 → V2 seam).
    ///
    /// TODO(V1/V2): on becoming leader —
    ///   - set role = Leader, `leader_id = self`;
    ///   - reinitialize `next_index[peer] = last_log_index + 1` and
    ///     `match_index[peer] = 0` for every peer;
    ///   - append a `Noop` entry in the new term and start replicating it, so the
    ///     leader can commit in its *own* term (the Raft "commit only current-term
    ///     entries" safety rule) and immediately start sending heartbeats.
    pub async fn become_leader(self: &Arc<Self>) {
        let _ = &self.inner;
        todo!("V1/V2: initialize leader state, append a no-op, and begin heartbeating")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove election safety —
    //   - a candidate with a stale log (older last term, or shorter) is denied;
    //   - a node grants at most one vote per term;
    //   - a single candidate with an up-to-date log wins a quorum and becomes leader;
    //   - seeing a higher term in any reply forces a step-down to follower.
}
