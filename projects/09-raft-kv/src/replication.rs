//! V2 — Log replication: keeping every node's log identical, and knowing when an
//! entry is safe to apply.
//!
//! Once elected, the leader is the only writer. A client command becomes a log
//! entry on the leader, which then pushes it to followers with `AppendEntries`.
//! The magic is the **consistency check**: every `AppendEntries` names the
//! `(prev_log_index, prev_log_term)` the new entries must follow. A follower
//! accepts only if it holds that exact entry; otherwise it rejects, the leader
//! walks `next_index` back, and retries — repairing a diverged tail. This yields
//! the **Log Matching** property: same index + same term ⇒ identical histories up
//! to there.
//!
//! **Commit** is the other half. An entry is committed once it's on a **majority**
//! — at which point the leader advances `commit_index`, and every node applies up
//! to it, in order, to the state machine (V3). The subtle safety rule: a leader
//! may only advance `commit_index` to an entry **from its own term**. Counting
//! replicas of a *previous* term's entry and committing it can lose data (Raft
//! §5.4.2) — the reason a new leader appends that `Noop` first.
//!
//! Guarantee delivered: **at-least-once apply of committed commands, in a total
//! order identical on every node.** (Exactly-once at the *client* needs request
//! dedup on top — the V3 stretch.)

use std::sync::Arc;

use crate::error::AppError;
use crate::node::RaftNode;
use crate::rpc::{AppendEntriesArgs, AppendEntriesReply, ClientResponse, Command};

impl RaftNode {
    /// Handle an inbound `AppendEntries` (V2) — the follower side of replication,
    /// and also the heartbeat that resets the election timer.
    ///
    /// TODO(V2): the follower's accept/reject logic —
    ///   - reply `false` if `args.term` < our term (stale leader);
    ///   - if `args.term` ≥ our term → adopt term, `become_follower`, record
    ///     `leader_id`, and **reset the election timer** (this is the heartbeat);
    ///   - consistency check: reply `false` unless we hold an entry at
    ///     `prev_log_index` with `prev_log_term` — optionally return a
    ///     `conflict_index` hint so the leader backs up fast;
    ///   - on match: delete any conflicting suffix, append the new `entries`,
    ///     then advance our `commit_index` to `min(leader_commit, last_new_index)`;
    ///   - `persist()` before replying `true`.
    pub async fn handle_append_entries(&self, args: AppendEntriesArgs) -> AppendEntriesReply {
        let _ = (&self.inner, &args);
        todo!("V2: run the consistency check, repair the log tail, advance commit_index")
    }

    /// Replicate to (or heartbeat) every peer once (V2). Called by the driver's
    /// heartbeat ticker while this node is leader.
    ///
    /// TODO(V2): for each peer, build an `AppendEntries` from `next_index[peer]`
    /// (entries from there on, with the matching `prev_log_*`), send it, and react:
    ///   - success → advance `match_index`/`next_index` for that peer, then try to
    ///     advance the leader's `commit_index` (see `maybe_advance_commit`);
    ///   - failure with a higher term → step down;
    ///   - failure on the consistency check → decrement `next_index` (or jump to
    ///     the `conflict_index` hint) and it retries next round;
    ///   - if `next_index` has fallen behind the snapshot, send `InstallSnapshot`
    ///     instead (V4).
    pub async fn broadcast_append_entries(self: &Arc<Self>) {
        let _ = &self.inner;
        todo!("V2: send AppendEntries to each peer and process the replies")
    }

    /// After replication progress, advance the leader's commit index if a majority
    /// now holds an entry **from the current term** (V2 safety rule).
    ///
    /// TODO(V2): find the highest index N such that `match_index` (counting self) ≥
    /// N on a `quorum()`, `log.term_at(N) == current_term`, and N >
    /// `commit_index`; set `commit_index = N`. Then let the apply path catch up.
    pub fn maybe_advance_commit(&self) {
        let _ = &self.inner;
        todo!("V2: advance commit_index to the highest quorum-replicated current-term entry")
    }

    /// Apply every newly-committed entry to the state machine, in order (V2 → V3).
    /// Called whenever `commit_index` moves, on leader and followers alike.
    ///
    /// TODO(V2/V3): while `last_applied` < `commit_index`, fetch the entry at
    /// `last_applied + 1`, `store.apply` it, and record the result for any client
    /// waiting on that index. This is the single point where the log becomes state.
    pub async fn apply_committed(&self) {
        let _ = (&self.inner, &self.store);
        todo!("V2/V3: apply committed entries to the Store in index order")
    }

    /// The client write path: propose a command, return once it is committed and
    /// applied (V2 → V3). Called by `PUT /kv/{key}` and `DELETE /kv/{key}`.
    ///
    /// TODO(V2): reject with `AppError::NotLeader` (with a leader hint) if not
    /// leader; otherwise append the command to the local log in the current term,
    /// `persist()`, kick replication, and **wait** until that index is committed
    /// and applied before returning its result. A leadership change while waiting
    /// must surface as `NotLeader`, not a hang.
    pub async fn propose(self: &Arc<Self>, command: Command) -> Result<ClientResponse, AppError> {
        let _ = (&self.inner, &command);
        todo!("V2: append to the leader's log, replicate to a quorum, wait for apply")
    }

    /// The linearizable read path (V3 read side, enforced here). Called by
    /// `GET /kv/{key}`.
    ///
    /// TODO(V2/V3): a correct read must not serve stale data. Reject if not leader;
    /// then confirm this node still leads *now* (a successful heartbeat round to a
    /// quorum, i.e. the read-index technique) and that the state machine has
    /// applied through the read index — only then read `store.get(key)`. Document
    /// the technique you chose (read-index vs. lease) in the design doc.
    pub async fn read(self: &Arc<Self>, key: &str) -> Result<Option<String>, AppError> {
        let _ = (&self.inner, key, &self.store);
        todo!("V2/V3: confirm leadership (read-index), then serve from applied state")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove replication safety —
    //   - the Log Matching property: after a round, follower logs equal the leader's
    //     at every (index, term);
    //   - a follower with a conflicting tail has it overwritten, not appended-past;
    //   - an entry replicated to a quorum becomes committed and applied everywhere;
    //   - a previous-term entry is NOT committed by replica-count alone (§5.4.2).
}
