//! V5 — Sticky workflow-state cache.
//!
//! Replay (V2) is correct but not free: rebuilding a long-running workflow's state
//! means folding its *entire* history on every single task. A workflow with 10,000
//! events would replay all 10,000 just to make its 10,001st decision. Temporal's fix is
//! **sticky execution**: after a worker runs a task, the engine routes that execution's
//! next task *back to the same worker*, which kept the folded [`WorkflowState`] in
//! memory. The worker then only needs the events since it last ran — a handful, not the
//! whole log.
//!
//! The catch — and the lesson — is that this cache lives in a *specific worker's*
//! memory, so it is only valid while that worker is alive and reachable. This module is
//! the engine-side routing table: which execution is pinned to which worker, and until
//! when. If the sticky worker doesn't poll in time (it crashed — The Reaper), the pin
//! expires and the execution falls back to the normal queue, where any worker picks it
//! up with a **full replay**. Correctness never depends on the cache; it only makes the
//! common case cheap.
//!
//! Cache state is process-local (a `Mutex<HashMap>`), not in Postgres — losing it costs
//! a replay, never correctness, so it must never be a second source of truth.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::model::RunId;

/// A live pin: this execution's next workflow task should go to `worker_identity`,
/// which cached the state through `last_event_id`, until `expires_at`.
#[derive(Debug, Clone)]
pub struct StickyPin {
    pub worker_identity: String,
    pub last_event_id: i64,
    pub expires_at: Instant,
}

/// The engine-side sticky routing table.
#[derive(Default)]
pub struct StickyCache {
    inner: Mutex<HashMap<RunId, StickyPin>>,
    ttl: Duration,
}

impl StickyCache {
    pub fn new(ttl: Duration) -> Self {
        Self {
            inner: Mutex::new(HashMap::new()),
            ttl,
        }
    }

    /// The configured stickiness window — how long a pin survives without the worker
    /// polling before the execution falls back to the normal queue.
    pub fn ttl(&self) -> Duration {
        self.ttl
    }

    /// Is this execution currently pinned to a live worker, and if so through which
    /// event id? Used at poll time to decide "sticky delta" vs "full replay".
    ///
    /// TODO(V5): look up `run_id`. Return the pin only if it hasn't expired (an
    /// expired pin means the worker went silent — treat it as gone and evict it).
    /// A hit lets the caller send just the events after `last_event_id`; a miss means a
    /// full-history workflow task on the normal queue.
    pub fn lookup(&self, run_id: RunId) -> Option<StickyPin> {
        let _ = (&self.inner, run_id, self.ttl);
        todo!("V5: return a live (non-expired) pin for this execution, if any")
    }

    /// Pin (or refresh) an execution to the worker that just ran it, recording how far
    /// its cached state now extends. Call this on RespondWorkflowTaskCompleted.
    ///
    /// TODO(V5): insert/replace the pin for `run_id` with `expires_at = now + ttl`.
    /// This is what makes the NEXT task sticky-eligible for `worker_identity`.
    pub fn pin(&self, run_id: RunId, worker_identity: &str, last_event_id: i64) {
        let _ = (
            &self.inner,
            run_id,
            worker_identity,
            last_event_id,
            self.ttl,
        );
        todo!("V5: pin an execution to the worker that cached its state")
    }

    /// Drop an execution's pin — the worker was lost, evicted, or the execution
    /// finished. The next poll for it will be a normal-queue full replay.
    ///
    /// TODO(V5): remove `run_id` from the table.
    pub fn evict(&self, run_id: RunId) {
        let _ = (&self.inner, run_id);
        todo!("V5: drop a pin so the execution falls back to full replay")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V5): this is pure in-memory logic — test it with NO database:
    //   - pin then lookup within the TTL is a hit carrying the right last_event_id;
    //   - lookup after the TTL elapses is a miss (and evicts the stale pin);
    //   - evict makes a subsequent lookup a miss;
    //   - pinning the same run to a new worker re-routes it (last writer wins).
}
