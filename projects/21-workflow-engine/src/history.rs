//! V1 — The event-sourced history log, from scratch.
//!
//! This is the part that makes a workflow *durable*. A workflow's state is not a row
//! you `UPDATE`; it is **derived** by replaying an append-only log of immutable
//! [`Event`]s (V2 does the folding — this module owns the log itself). Every fact the
//! engine knows about an execution — it started, an activity was scheduled, a timer
//! fired, it completed — is an event appended here, in order, forever.
//!
//! Why event-sourcing and not "a status column you mutate"? Because a mutable status
//! can only tell you *where you are*, never *how you got there* — and "how you got
//! there" is exactly what a fresh worker needs to resume a half-finished execution
//! after a crash. The log IS the state; a projection (the `workflow_executions.status`
//! column) is a convenience you may keep in sync, never the source of truth.
//!
//! The two invariants this module owns:
//!   1. **Monotonic + gapless:** `event_id` is 1, 2, 3, … per run; an append that
//!      skips or reuses an id is a bug the (run_id, event_id) primary key rejects.
//!   2. **Append-only:** no code path updates or deletes a posted event.

use sqlx::PgPool;

use crate::error::AppError;
use crate::model::{Event, RunId, WorkflowId};

/// Everything StartWorkflow needs to open a fresh history.
#[derive(Debug, Clone)]
pub struct StartOptions {
    pub workflow_id: WorkflowId,
    pub workflow_type: String,
    pub task_queue: String,
    pub input: Vec<u8>,
}

/// The durable event log, backed by the `workflow_executions` + `history_events` tables.
pub struct HistoryStore {
    pool: PgPool,
}

impl HistoryStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// The connection pool, for callers that run their own transaction spanning
    /// history + task-queue + timer writes (the dispatcher, the timer service).
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Open a new execution: insert the `workflow_executions` row and append the very
    /// first event, `WORKFLOW_STARTED` (carrying the input), as event id 1.
    ///
    /// TODO(V1): in ONE transaction, INSERT the execution (status = 'running') and
    /// INSERT its `WORKFLOW_STARTED` event (event_id = 1). Return the new run id. The
    /// engine will then schedule the first workflow task (that's the dispatcher's job).
    pub async fn start_execution(&self, opts: StartOptions) -> Result<RunId, AppError> {
        let _ = (&self.pool, opts);
        todo!("V1: create an execution and append WORKFLOW_STARTED as event 1")
    }

    /// Append `events` to a run's history **atomically and in order**.
    ///
    /// TODO(V1): INSERT every event for `run_id` in one transaction. The caller assigns
    /// `event_id`s (from the replayed [`WorkflowState::next_event_id`]); your job is to
    /// make the whole batch land or none of it, so a partial append can never corrupt a
    /// history. A duplicate `event_id` must FAIL (the primary key enforces this) — that
    /// collision is how you'd catch two workers trying to advance the same execution.
    pub async fn append_events(&self, run_id: RunId, events: &[Event]) -> Result<(), AppError> {
        let _ = (&self.pool, run_id, events);
        todo!("V1: atomically append an ordered batch of events")
    }

    /// Load a run's **entire** history, ordered by `event_id` — what a non-sticky
    /// worker replays to rebuild state from scratch (V2).
    ///
    /// TODO(V1): SELECT * FROM history_events WHERE run_id = $1 ORDER BY event_id.
    /// Decode each row (event_type via [`crate::model::EventType::from_db_str`]).
    pub async fn load_history(&self, run_id: RunId) -> Result<Vec<Event>, AppError> {
        let _ = (&self.pool, run_id);
        todo!("V1: load a run's full ordered history")
    }

    /// Load only the events with `event_id > after_event_id` — the delta a sticky
    /// worker (V5) needs to catch up its cached state without re-reading the whole log.
    ///
    /// TODO(V1): the same query as [`load_history`], plus `AND event_id > $2`.
    pub async fn load_history_after(
        &self,
        run_id: RunId,
        after_event_id: i64,
    ) -> Result<Vec<Event>, AppError> {
        let _ = (&self.pool, run_id, after_event_id);
        todo!("V1: load a run's history after a given event id (sticky delta)")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the log (these want a real Postgres — gate them behind a
    // DATABASE_URL fixture). Suggested cases:
    //   - start_execution writes event 1 = WORKFLOW_STARTED and a 'running' row;
    //   - append_events assigns 2,3,4… and load_history reads them back IN ORDER;
    //   - appending a duplicate event_id FAILS and writes nothing (atomic batch);
    //   - load_history_after(run, k) returns exactly the events with id > k.
}
