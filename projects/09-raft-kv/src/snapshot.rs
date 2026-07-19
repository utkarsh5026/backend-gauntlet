//! V4 — Snapshots & log compaction: keeping the log from growing forever.
//!
//! An append-only log grows without bound, and replaying it from index 1 on every
//! restart gets slower forever. The fix: periodically **snapshot** the state
//! machine (the whole KV map is far smaller than the history that produced it),
//! then **discard** every log entry the snapshot covers. The snapshot records the
//! `last_included_index` / `last_included_term` it replaces, so the log's
//! consistency checks still line up at that boundary.
//!
//! This also changes replication. A leader that has compacted past what a slow
//! follower needs can no longer send those entries — they're gone. So it sends the
//! **whole snapshot** via `InstallSnapshot`; the follower adopts it wholesale and
//! resumes from `last_included_index + 1`.
//!
//! The trap: compaction races with everything. You may only discard entries at or
//! below `last_applied` (never un-applied or un-committed ones), and the snapshot
//! must be durable before the log is truncated — or a crash mid-compaction loses
//! committed state.

use std::sync::Arc;

use crate::error::AppError;
use crate::node::RaftNode;
use crate::rpc::{InstallSnapshotArgs, InstallSnapshotReply};

impl RaftNode {
    /// Take a snapshot and compact the log if it has grown past the threshold (V4).
    /// Called from the apply path after `last_applied` advances.
    ///
    /// TODO(V4): if `log.len()` exceeds `config.snapshot_threshold`, capture
    /// `(last_applied, term_at(last_applied))`, serialize the machine
    /// (`store.snapshot()`), persist the snapshot durably, then compact the log up
    /// to that index (drop entries, set the snapshot base). Order matters:
    /// snapshot durable *before* truncation.
    pub async fn maybe_snapshot(self: &Arc<Self>) -> Result<(), AppError> {
        let _ = (&self.inner, &self.store, &self.config);
        todo!("V4: snapshot the state machine and compact the log past last_applied")
    }

    /// Handle an inbound `InstallSnapshot` (V4) — a leader is handing us a snapshot
    /// because the entries we need have been compacted away on its side.
    ///
    /// TODO(V4): reject if `args.term` < our term; otherwise adopt the term / step
    /// down and reset the election timer, then — if the snapshot is newer than our
    /// state — `store.restore(&args.data)`, replace our log with an empty one based
    /// at `last_included_index`, set `commit_index`/`last_applied` to it, and
    /// persist. Reply with our current term.
    pub async fn handle_install_snapshot(&self, args: InstallSnapshotArgs) -> InstallSnapshotReply {
        let _ = (&self.inner, &self.store, &args);
        todo!("V4: install the leader's snapshot, replacing state + log base")
    }

    /// Ship the current snapshot to a lagging `peer` whose `next_index` has fallen
    /// below the snapshot boundary (V4). Called from replication when plain
    /// `AppendEntries` can no longer reach that far back.
    ///
    /// TODO(V4): build `InstallSnapshotArgs` from the persisted snapshot, send it,
    /// and on success set that peer's `next_index`/`match_index` to just past
    /// `last_included_index`.
    pub async fn send_snapshot(self: &Arc<Self>, peer: crate::rpc::NodeId) {
        let _ = (&self.inner, peer);
        todo!("V4: send the snapshot to a follower that has fallen behind the compacted log")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove compaction is safe —
    //   - after a snapshot, the log's physical length drops but reads still resolve
    //     (via the snapshot base) and applied state is unchanged;
    //   - a follower far behind is caught up by InstallSnapshot and then resumes
    //     normal AppendEntries from last_included_index + 1;
    //   - a node restarting from a snapshot + tail recovers identical state.
}
