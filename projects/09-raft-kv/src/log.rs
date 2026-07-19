//! The persistent replicated log — the durable spine of consensus.
//!
//! Raft splits a node's state into **persistent** (must survive a crash before a
//! reply is sent: `current_term`, `voted_for`, and the log entries) and
//! **volatile** (rebuilt on restart: commit index, roles, leader bookkeeping).
//! This module owns the persistent part's storage: the ordered list of
//! [`LogEntry`]s plus the metadata, and the job of getting them onto disk.
//!
//! Why persistence is not optional: Raft's safety proof assumes that once a node
//! votes in a term, or acknowledges an entry, it *remembers* that across a
//! restart. A node that forgets its vote can vote twice in one term and elect two
//! leaders. So `persist` must make the bytes durable (fsync) **before** the RPC
//! handler replies — that ordering is V1/V2 work, wired here as a `todo!()`.
//!
//! Indexing note: entries are **1-based** (index 0 = empty). Once snapshots (V4)
//! compact a prefix away, `snapshot_last_index` records where the physical
//! `entries` vec now begins, so a logical index maps to a vec position by
//! subtracting it. The scaffold keeps that base at 0 (no snapshot yet).

use std::path::PathBuf;

use crate::error::AppError;
use crate::rpc::{Command, LogEntry, LogIndex, NodeId, Term};

/// A node's persistent state: the log plus the two metadata fields Raft requires
/// to be durable. Kept together because they must be persisted together.
pub struct RaftLog {
    /// Where the persisted state lives on disk (one file/dir per node).
    path: PathBuf,

    // --- persistent metadata ---
    /// Latest term this node has seen (starts at 0, only increases).
    current_term: Term,
    /// Candidate this node voted for in `current_term`, if any. Reset each term.
    voted_for: Option<NodeId>,

    // --- the log itself ---
    /// Log entries with logical index > `snapshot_last_index`, in order.
    entries: Vec<LogEntry>,
    /// The last index included in the most recent snapshot (V4). Everything at or
    /// below it has been compacted out of `entries`. 0 until the first snapshot.
    snapshot_last_index: LogIndex,
    /// The term of `snapshot_last_index` — needed for the consistency check when
    /// `prev_log_index` falls exactly on the snapshot boundary.
    snapshot_last_term: Term,
}

impl RaftLog {
    /// Load persisted state from `path`, or start empty if there is none.
    ///
    /// Plumbing creates the directory. **Recovery** — reading back
    /// `current_term`, `voted_for`, and every entry so a restarted node resumes
    /// exactly where it left off — is the durability half of V1/V2.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, AppError> {
        let path = path.into();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }

        // TODO(V1/V2 durability): if a persisted file exists at `path`, decode it
        // and restore current_term / voted_for / entries (and the snapshot base,
        // V4). For now start empty; `persist` is where the write half goes.
        Ok(Self {
            path,
            current_term: 0,
            voted_for: None,
            entries: Vec::new(),
            snapshot_last_index: 0,
            snapshot_last_term: 0,
        })
    }

    /// Durably write the persistent state (term, vote, log) so it survives a
    /// crash. Must complete **before** the caller replies to an RPC.
    ///
    /// TODO(V1/V2): serialize the persistent fields and write+fsync them to
    /// `self.path` (write-to-temp + rename, or an append-only record — your call,
    /// documented). The throughput cost of fsync-per-write vs. batching is the
    /// same dial as project 08's log.
    pub fn persist(&self) -> Result<(), AppError> {
        let _ = &self.path;
        todo!("V1/V2: durably persist current_term, voted_for and the log entries")
    }

    // ---- persistent metadata accessors (wired — trivial) -------------------

    pub fn current_term(&self) -> Term {
        self.current_term
    }

    /// Set the current term and clear the vote (a new term means a fresh ballot).
    /// The caller must `persist()` before acting on it.
    pub fn set_current_term(&mut self, term: Term) {
        self.current_term = term;
        self.voted_for = None;
    }

    pub fn voted_for(&self) -> Option<NodeId> {
        self.voted_for
    }

    pub fn set_voted_for(&mut self, candidate: Option<NodeId>) {
        self.voted_for = candidate;
    }

    // ---- log geometry (wired — index math the consensus code leans on) -----

    /// The index of the last entry (0 if the log is empty and un-snapshotted).
    pub fn last_index(&self) -> LogIndex {
        match self.entries.last() {
            Some(e) => e.index,
            None => self.snapshot_last_index,
        }
    }

    /// The term of the last entry — half of the "up-to-date" comparison a voter
    /// makes in V1.
    pub fn last_term(&self) -> Term {
        match self.entries.last() {
            Some(e) => e.term,
            None => self.snapshot_last_term,
        }
    }

    /// The term of the entry at `index`, if the log holds it. `None` if `index` is
    /// beyond the tail; the snapshot boundary answers for `snapshot_last_index`.
    pub fn term_at(&self, index: LogIndex) -> Option<Term> {
        if index == self.snapshot_last_index && index != 0 {
            return Some(self.snapshot_last_term);
        }
        self.get(index).map(|e| e.term)
    }

    /// Borrow the entry at logical `index`, if present in `entries`.
    pub fn get(&self, index: LogIndex) -> Option<&LogEntry> {
        if index <= self.snapshot_last_index {
            return None;
        }
        let pos = (index - self.snapshot_last_index - 1) as usize;
        self.entries.get(pos)
    }

    /// Every entry at index ≥ `from` (cloned) — what the leader ships in an
    /// `AppendEntries` to a follower that needs them (V2).
    pub fn entries_from(&self, from: LogIndex) -> Vec<LogEntry> {
        let from = from.max(self.snapshot_last_index + 1);
        let start = (from - self.snapshot_last_index - 1) as usize;
        self.entries
            .get(start..)
            .map(<[_]>::to_vec)
            .unwrap_or_default()
    }

    /// Append entries to the tail. Wired mechanics; *when* it is safe to call, and
    /// how conflicts are resolved before it, is the V2 logic in `replication.rs`.
    pub fn append(&mut self, mut new_entries: Vec<LogEntry>) {
        self.entries.append(&mut new_entries);
    }

    /// Drop every entry at index ≥ `from` — used when a follower's tail conflicts
    /// with the leader's and must be overwritten (V2). Committed entries must
    /// never reach here; that invariant is the caller's to uphold.
    pub fn truncate_from(&mut self, from: LogIndex) {
        if from <= self.snapshot_last_index {
            self.entries.clear();
            return;
        }
        let pos = (from - self.snapshot_last_index - 1) as usize;
        self.entries.truncate(pos);
    }

    /// The snapshot boundary (V4): the last index the state machine has folded
    /// into a snapshot, and its term.
    pub fn snapshot_point(&self) -> (LogIndex, Term) {
        (self.snapshot_last_index, self.snapshot_last_term)
    }

    /// Number of entries physically retained (post-compaction) — the signal the
    /// snapshot trigger (V4) watches.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// Build a `Set` command — small convenience the client-write path uses.
pub fn set(key: impl Into<String>, value: impl Into<String>) -> Command {
    Command::Set {
        key: key.into(),
        value: value.into(),
    }
}
