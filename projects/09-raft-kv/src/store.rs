//! V3 — The replicated key-value state machine.
//!
//! Consensus (V1 + V2) produces one thing: an agreed-upon, totally-ordered
//! sequence of committed [`Command`]s, identical on every node. This module is
//! what *consumes* that sequence. `apply` folds each committed command into an
//! in-memory `HashMap`, **in log order, exactly once, on every node** — which is
//! why every node's map ends up identical. That determinism is the whole payoff
//! of Raft: the log is the source of truth, the map is a cache of "the log,
//! reduced".
//!
//! The subtle part isn't `Set`/`Delete` — it's the two guarantees around them:
//!   - **Apply order == commit order, never skipping.** `apply` may only run for
//!     `last_applied + 1`. Applying out of order, or twice, silently diverges
//!     nodes. That sequencing is driven from the replication layer (V2).
//!   - **Linearizable reads.** A `GET` that just reads this map from a follower
//!     can return stale data (that node may be behind, or a deposed leader). A
//!     linearizable read must be served by a leader that has confirmed it still
//!     leads (heartbeat round / read-index). That check lives on the read path
//!     (`RaftNode::read`), not here — this map only ever reflects applied state.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::rpc::{Command, LogEntry, LogIndex};

/// The deterministic state machine behind the KV API.
pub struct Store {
    inner: Mutex<Inner>,
}

struct Inner {
    data: HashMap<String, String>,
    /// The index of the last log entry folded into `data`. The invariant
    /// `last_applied` advances by exactly 1 per `apply` is what keeps nodes in sync.
    last_applied: LogIndex,
}

impl Store {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                data: HashMap::new(),
                last_applied: 0,
            }),
        }
    }

    /// The highest log index this machine has applied.
    pub fn last_applied(&self) -> LogIndex {
        self.inner.lock().unwrap().last_applied
    }

    /// Apply one committed entry, returning the prior value if the command replaced
    /// or removed one. **The core of V3.**
    ///
    /// TODO(V3): match on `entry.command` — `Set` inserts, `Delete` removes, `Noop`
    /// does nothing — and advance `last_applied` to `entry.index`. Enforce the
    /// sequencing invariant: reject (or debug-assert) an entry whose index isn't
    /// exactly `last_applied + 1`, so a caller can never apply out of order.
    /// **Stretch:** dedupe by a per-client request id so a retried command (Raft is
    /// at-least-once) isn't applied twice.
    pub fn apply(&self, entry: &LogEntry) -> Option<String> {
        let _ = (&self.inner, entry, Command::Noop);
        todo!("V3: apply this committed command to the map and bump last_applied")
    }

    /// Read the current value for `key` from applied state.
    ///
    /// Wired: this is just the map lookup. Whether it is *safe* to serve this to a
    /// client (leadership confirmed, applied caught up) is decided upstream on the
    /// read path — see the V3 linearizability note in the module docs.
    pub fn get(&self, key: &str) -> Option<String> {
        self.inner.lock().unwrap().data.get(key).cloned()
    }

    /// Serialize the whole machine into a snapshot blob (V4). Used when the log has
    /// grown enough that compacting it is worthwhile.
    ///
    /// TODO(V4): encode `data` + `last_applied` into bytes for
    /// `InstallSnapshot` / on-disk compaction.
    pub fn snapshot(&self) -> Vec<u8> {
        let _ = &self.inner;
        todo!("V4: serialize the state machine (data + last_applied) into a snapshot")
    }

    /// Replace the machine wholesale from a snapshot blob (V4) — what a follower
    /// does on `InstallSnapshot`.
    ///
    /// TODO(V4): decode `bytes` and overwrite `data` + `last_applied`.
    pub fn restore(&self, bytes: &[u8]) {
        let _ = (&self.inner, bytes);
        todo!("V4: restore the state machine from a snapshot blob")
    }
}

impl Default for Store {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the state machine —
    //   - applying Set/Delete in order yields the expected map;
    //   - applying the same committed log on two fresh Stores yields identical maps
    //     (determinism — the property that makes replication meaningful);
    //   - apply refuses a gap (index != last_applied + 1).
}
