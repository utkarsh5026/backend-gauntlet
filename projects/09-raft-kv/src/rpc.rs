//! Shared vocabulary — the ids, the log entry, and the four wire messages every
//! node speaks to every other node.
//!
//! Plumbing, not a vertical: these are plain, serializable data. They're grouped
//! here (the "record.rs" of this project) so `log.rs` can stay purely about
//! *storing* entries and the consensus modules can stay about *deciding* things.
//!
//! Two RPCs carry the whole protocol — `RequestVote` (elections, V1) and
//! `AppendEntries` (replication *and* heartbeats, V2) — plus `InstallSnapshot`
//! (V4) for a follower that has fallen behind the leader's compacted log.

use serde::{Deserialize, Serialize};

/// A node's identity in the cluster. Small, stable, assigned by config.
pub type NodeId = u64;
/// A Raft *term* — a logical clock that only ever increases. Every message
/// carries one; the higher term always wins, and seeing a higher term forces a
/// node back to follower. This single rule is what makes the protocol safe.
pub type Term = u64;
/// A 1-based position in the replicated log. Index 0 means "empty log".
pub type LogIndex = u64;

/// A command the state machine (V3) can apply. This is what a log entry carries
/// and what a client write turns into. `Noop` is the entry a fresh leader appends
/// on election so it can safely advance its commit index in its own term.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum Command {
    Set { key: String, value: String },
    Delete { key: String },
    Noop,
}

/// One entry in the replicated log: a command stamped with the term it was
/// created in and its index. The `(term, index)` pair is the heart of the Log
/// Matching property (V2) — if two logs hold an entry with the same term at the
/// same index, they agree on everything before it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEntry {
    pub term: Term,
    pub index: LogIndex,
    pub command: Command,
}

// ---- RequestVote (V1 — elections) ------------------------------------------

/// Sent by a candidate to gather votes. A peer grants its vote at most once per
/// term, and only to a candidate whose log is *at least as up-to-date* as its own.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RequestVoteArgs {
    pub term: Term,
    pub candidate_id: NodeId,
    pub last_log_index: LogIndex,
    pub last_log_term: Term,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RequestVoteReply {
    /// The voter's current term, so a stale candidate learns it has been left behind.
    pub term: Term,
    pub vote_granted: bool,
}

// ---- AppendEntries (V2 — replication + heartbeat) --------------------------

/// Sent by the leader to replicate entries — and, with an empty `entries`, as the
/// heartbeat that suppresses new elections. `prev_log_*` is the consistency
/// check: the follower only accepts if it has that exact entry, which is how a
/// diverged follower's log gets walked back and repaired.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppendEntriesArgs {
    pub term: Term,
    pub leader_id: NodeId,
    pub prev_log_index: LogIndex,
    pub prev_log_term: Term,
    pub entries: Vec<LogEntry>,
    /// The leader's commit index, so followers learn what's safe to apply.
    pub leader_commit: LogIndex,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppendEntriesReply {
    pub term: Term,
    pub success: bool,
    /// Optional fast-backup hint: the index the leader should retry from when the
    /// consistency check fails, so it doesn't decrement `next_index` one at a time.
    #[serde(default)]
    pub conflict_index: Option<LogIndex>,
}

// ---- InstallSnapshot (V4 — catching up a lagging follower) -----------------

/// Sent when the entries a follower needs have already been compacted away into a
/// snapshot. The follower adopts the snapshot wholesale and discards its log up to
/// `last_included_index`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallSnapshotArgs {
    pub term: Term,
    pub leader_id: NodeId,
    pub last_included_index: LogIndex,
    pub last_included_term: Term,
    /// The serialized state-machine snapshot. Sent whole here; chunking it is a
    /// stretch goal.
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallSnapshotReply {
    pub term: Term,
}

/// What a committed client command returns once it has been applied (V3): the
/// prior value for a `Set`/`Delete`, if any.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClientResponse {
    pub value: Option<String>,
}
