//! V2 — Deterministic replay: fold a history into state.
//!
//! A workflow's state is not stored; it is **recomputed** by folding its history
//! left-to-right. [`replay`] is that fold — a *pure function* from `&[Event]` to
//! [`WorkflowState`]. "Pure" is the entire point: replay the same events on any worker,
//! at any time, and you MUST get byte-identical state. That determinism is what lets a
//! crashed execution resume on a different machine as if nothing happened.
//!
//! This is also why workflow *code* has rules (the worker enforces them, but the
//! engine must understand them): a workflow may not read the wall clock, generate a
//! random number, or hit the network directly — every such effect would produce a
//! different result on replay. Instead the workflow issues [`Command`]s and the effects
//! come back as *recorded events*, which replay deterministically.
//!
//! The server's stake in determinism is [`check_determinism`]: when a worker reports
//! the commands it produced, the engine confirms they are consistent with the history
//! the worker was told to replay. A worker whose code changed (or is non-deterministic)
//! will emit commands that don't match — and the engine catches it here rather than
//! silently corrupting the execution.

use crate::error::AppError;
use crate::model::{Command, Event, WorkflowState};

/// Fold a full history into the current [`WorkflowState`]. Pure and deterministic:
/// no clock, no IO, no randomness — only the events decide the result.
///
/// TODO(V2): start from [`WorkflowState::initial`] and apply each event in `event_id`
/// order. Each event type moves the state:
///   - WORKFLOW_STARTED        → running; stash the input if you need it.
///   - ACTIVITY_SCHEDULED      → insert a PendingActivity keyed by the event id.
///   - ACTIVITY_COMPLETED/FAILED → remove the matching PendingActivity.
///   - TIMER_STARTED           → record started_timers[timer_id] = fire_at.
///   - TIMER_FIRED             → remove it.
///   - WORKFLOW_COMPLETED      → status = Completed, set `result`.
///   - WORKFLOW_FAILED         → status = Failed, set `failure`.
///   - (task scheduled/started/completed events advance bookkeeping only.)
/// Keep `next_event_id` = 1 + the highest event_id seen. Reject an out-of-order or
/// duplicate event id — a gap means a corrupt history, not something to paper over.
pub fn replay(history: &[Event]) -> Result<WorkflowState, AppError> {
    let _ = history;
    todo!("V2: fold the event history into a WorkflowState — purely, deterministically")
}

/// Confirm a worker's returned `commands` are consistent with the history it replayed.
///
/// When a worker finishes a workflow task it hands back the commands its (replayed)
/// code produced. If this execution has been advanced before, history already *records*
/// what those commands must be — so a replay that produces different commands is
/// non-determinism, and this is where the engine catches it.
///
/// TODO(V2): reconstruct the state at `replayed_through` (fold `history` up to that
/// event id), then check the `commands` against what the recorded events after that
/// point imply. On a mismatch, return [`AppError::NonDeterministic`] naming the first
/// divergence (expected X, got Y) — that message is a workflow author's best debugging
/// clue. On a first-ever task (no later events yet) there is nothing to contradict, so
/// any commands are accepted and become the record.
pub fn check_determinism(
    history: &[Event],
    replayed_through: i64,
    commands: &[Command],
) -> Result<(), AppError> {
    let _ = (history, replayed_through, commands);
    todo!("V2: detect a worker whose replay diverged from recorded history")
}

#[cfg(test)]
mod tests {
    // TODO(V2): replay is a pure function — test it with NO database, just Vec<Event>.
    // Suggested cases:
    //   - replay([]) == WorkflowState::initial();
    //   - a start→schedule-activity→activity-completed→complete history folds to a
    //     Completed state with the right result and no pending activities;
    //   - a proptest: for any valid history, replaying it once == replaying it twice
    //     (idempotent) AND == replaying it split into two halves then merged
    //     (batching must not matter) — see `prop_replay_is_deterministic`;
    //   - check_determinism flags commands that contradict a recorded event.
}
