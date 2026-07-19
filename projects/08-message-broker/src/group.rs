//! V4 — Consumer groups & durable offset commits: at-least-once delivery.
//!
//! A consumer group is a set of members that *share* one cursor per partition and
//! *split* the topic's partitions between them. Two things make it a group and not
//! just a reader:
//!   1. **Durable committed offsets.** For each `(group, topic, partition)` the
//!      coordinator stores how far the group has read, and it survives a broker
//!      restart — so a returning consumer resumes from the commit, not from 0.
//!      Different groups keep independent commits over the same topic.
//!   2. **Assignment.** Each partition is owned by at most one member at a time;
//!      a member joining or leaving triggers a reassignment that keeps every
//!      partition covered.
//!
//! Delivery is **at-least-once** because a consumer commits *after* it processes:
//! die in between and the next fetch re-reads from the last commit — redelivery,
//! never silent loss. That commit ordering is the guarantee, and it's a `Done
//! when` criterion + a design-doc line.
//!
//! The committed offsets are the broker's own durable state, so they live on disk
//! under `groups/` — the same append-only discipline as V1 (Kafka stores them in
//! an internal `__consumer_offsets` topic; a file per group is the learning stand-in).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::error::AppError;
use crate::record::Offset;

/// The partitions a member is told to consume after a join/rebalance.
#[derive(Debug, Clone)]
pub struct Assignment {
    pub partitions: Vec<u32>,
}

/// Owns every consumer group's durable committed offsets and live membership.
pub struct GroupCoordinator {
    dir: PathBuf,
    /// In-memory view of all groups, keyed by group name. Guards both the
    /// committed offsets and the current member list. Loaded from `dir` on open.
    groups: Mutex<HashMap<String, GroupState>>,
}

/// Per-group state: committed offsets + who's currently in the group.
#[derive(Debug, Default)]
struct GroupState {
    /// (topic, partition) → committed offset. The durable bookmark.
    committed: HashMap<(String, u32), Offset>,
    /// Current member ids (for assignment).
    members: Vec<String>,
}

impl GroupCoordinator {
    /// Open the coordinator, creating `dir` if needed.
    ///
    /// Plumbing creates the directory. **Loading** the committed offsets back into
    /// memory (so a restart resumes each group) is V4 recovery work.
    pub fn open(dir: impl Into<PathBuf>) -> Result<Arc<Self>, AppError> {
        let dir = dir.into();
        std::fs::create_dir_all(&dir)?;
        // TODO(V4 recovery): read each group's persisted committed offsets under
        // `dir` back into `groups`, so a restart resumes where every group left
        // off. Starting empty here means a restart currently forgets commits —
        // that regression is exactly what your restart test should catch.
        Ok(Arc::new(Self {
            dir,
            groups: Mutex::new(HashMap::new()),
        }))
    }

    /// Commit the group's progress for one `(topic, partition)`. Must be durable:
    /// a crash *after* this returns must not lose the commit.
    pub async fn commit(
        &self,
        group: &str,
        topic: &str,
        partition: u32,
        offset: Offset,
    ) -> Result<(), AppError> {
        // TODO(V4): update the in-memory `committed` map AND persist it to `dir`
        // (fsync) before returning — an unpersisted commit that a restart forgets
        // breaks the "resume from commit" guarantee. Guard against an offset going
        // backwards if you want commits to be monotonic.
        let _ = (group, topic, partition, offset, &self.dir, &self.groups);
        todo!("V4: durably record a group's committed offset for a partition")
    }

    /// The group's committed offset for `(topic, partition)`, or `None` if the
    /// group has never committed there (a fresh consumer starts from 0 or the log
    /// start, your policy).
    pub async fn committed(
        &self,
        group: &str,
        topic: &str,
        partition: u32,
    ) -> Result<Option<Offset>, AppError> {
        // TODO(V4): read the committed offset from `groups`.
        let _ = (group, topic, partition, &self.groups);
        todo!("V4: look up a group's committed offset")
    }

    /// A member joins the group for `topic` (which has `partition_count`
    /// partitions); returns the partitions it should now own.
    pub async fn join(
        &self,
        group: &str,
        member: &str,
        topic: &str,
        partition_count: u32,
    ) -> Result<Assignment, AppError> {
        // TODO(V4): add `member` to the group, then recompute the assignment so
        // the `partition_count` partitions are split across all current members
        // with each partition owned by exactly one member; return this member's
        // share. (A rebalance also changes what the *other* members own — model
        // that however you expose reassignment.)
        let _ = (group, member, topic, partition_count, &self.groups);
        todo!("V4: add the member and assign it a disjoint slice of partitions")
    }

    /// A member leaves the group; its partitions are reassigned to the rest.
    pub async fn leave(&self, group: &str, member: &str) -> Result<(), AppError> {
        // TODO(V4): remove `member` and rebalance so its partitions get covered by
        // the remaining members (no partition left unowned while members exist).
        let _ = (group, member, &self.groups);
        todo!("V4: remove the member and rebalance its partitions")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the group semantics —
    //   - commit then reopen the coordinator → committed() still returns it
    //     (durable, survives restart);
    //   - two members of one group over N partitions get disjoint assignments
    //     that together cover 0..N (exclusive ownership);
    //   - two *different* groups over the same topic keep independent committed
    //     offsets;
    //   - "process then crash before commit" leaves the committed offset behind,
    //     so the next fetch redelivers — at-least-once, not loss.
}
