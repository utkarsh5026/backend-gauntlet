//! Shared vocabulary for the workflow engine.
//!
//! Every vertical speaks in these terms: the [`Event`]s that make up a history (V1),
//! the [`WorkflowState`] a [replay](crate::replay) folds them into (V2), the
//! [`Command`]s a worker returns, and the [`TaskToken`] that authorizes one response.
//! These are fully-implemented value types — the exception to "don't write the meat"
//! — so the interesting modules can stay about *behavior*, not vocabulary.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// The caller's logical workflow id (chosen at StartWorkflow).
pub type WorkflowId = String;

/// Names one execution *attempt* of a workflow. A UUID, so starting a workflow needs
/// no coordinated sequence.
pub type RunId = Uuid;

/// The kind of a history event. A history is an append-only log of these; folding it
/// left-to-right reconstructs the execution's state (V1/V2). Mirrors the protobuf
/// `EventType`, but this is the internal type the store and replayer use — it also
/// knows how to render itself for the `event_type` TEXT column.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum EventType {
    WorkflowStarted,
    WorkflowTaskScheduled,
    WorkflowTaskStarted,
    WorkflowTaskCompleted,
    ActivityScheduled,
    ActivityStarted,
    ActivityCompleted,
    ActivityFailed,
    TimerStarted,
    TimerFired,
    WorkflowCompleted,
    WorkflowFailed,
}

impl EventType {
    /// The value stored in the `history_events.event_type` column.
    pub fn as_db_str(&self) -> &'static str {
        match self {
            EventType::WorkflowStarted => "workflow_started",
            EventType::WorkflowTaskScheduled => "workflow_task_scheduled",
            EventType::WorkflowTaskStarted => "workflow_task_started",
            EventType::WorkflowTaskCompleted => "workflow_task_completed",
            EventType::ActivityScheduled => "activity_scheduled",
            EventType::ActivityStarted => "activity_started",
            EventType::ActivityCompleted => "activity_completed",
            EventType::ActivityFailed => "activity_failed",
            EventType::TimerStarted => "timer_started",
            EventType::TimerFired => "timer_fired",
            EventType::WorkflowCompleted => "workflow_completed",
            EventType::WorkflowFailed => "workflow_failed",
        }
    }

    /// Parse the value read back from the `event_type` column.
    pub fn from_db_str(s: &str) -> Option<Self> {
        Some(match s {
            "workflow_started" => EventType::WorkflowStarted,
            "workflow_task_scheduled" => EventType::WorkflowTaskScheduled,
            "workflow_task_started" => EventType::WorkflowTaskStarted,
            "workflow_task_completed" => EventType::WorkflowTaskCompleted,
            "activity_scheduled" => EventType::ActivityScheduled,
            "activity_started" => EventType::ActivityStarted,
            "activity_completed" => EventType::ActivityCompleted,
            "activity_failed" => EventType::ActivityFailed,
            "timer_started" => EventType::TimerStarted,
            "timer_fired" => EventType::TimerFired,
            "workflow_completed" => EventType::WorkflowCompleted,
            "workflow_failed" => EventType::WorkflowFailed,
            _ => return None,
        })
    }

    /// Is this a terminal event — the last one a history can ever have?
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            EventType::WorkflowCompleted | EventType::WorkflowFailed
        )
    }
}

/// One entry in an execution's history. `event_id` is monotonic per run and defines
/// the replay order; `attributes` carries the event-type-specific payload (activity
/// type + input, timer id + fire time, the workflow result, …).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub event_id: i64,
    pub event_type: EventType,
    pub timestamp_ms: i64,
    pub attributes: serde_json::Value,
}

impl Event {
    pub fn new(event_id: i64, event_type: EventType, attributes: serde_json::Value) -> Self {
        Self {
            event_id,
            event_type,
            timestamp_ms: now_ms(),
            attributes,
        }
    }
}

/// A decision the workflow made on its most recent task, decoded from the wire. The
/// server validates the command stream against history (determinism, V2), then turns
/// each command into events + the side effects it implies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// Run an activity out-of-process on a worker.
    ScheduleActivity {
        activity_type: String,
        input: Vec<u8>,
    },
    /// Start a durable timer that fires `delay_ms` from now (V3).
    StartTimer { timer_id: String, delay_ms: i64 },
    /// Finish the workflow successfully with this result.
    CompleteWorkflow { result: Vec<u8> },
    /// Finish the workflow with a failure.
    FailWorkflow { failure: String },
}

/// Where an execution is in its lifecycle. `Running` is every non-terminal state; the
/// replayer sets `Completed`/`Failed` only when it folds the terminal event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionStatus {
    Running,
    Completed,
    Failed,
}

/// An activity that has been scheduled but not yet resolved, keyed in
/// [`WorkflowState`] by its `ACTIVITY_SCHEDULED` event id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingActivity {
    pub activity_type: String,
    pub input: Vec<u8>,
}

/// The state produced by folding a history (V2). It is *derived*, never stored-and-
/// mutated: two replays of the same events must yield an identical `WorkflowState`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkflowState {
    pub status: ExecutionStatus,
    /// The id the *next* appended event will take (1 + the highest seen).
    pub next_event_id: i64,
    /// Scheduled-but-unresolved activities, keyed by their schedule event id.
    pub pending_activities: BTreeMap<i64, PendingActivity>,
    /// Started-but-unfired timers, keyed by workflow-assigned `timer_id` → fire epoch ms.
    pub started_timers: BTreeMap<String, i64>,
    /// Set once the workflow completes.
    pub result: Option<Vec<u8>>,
    /// Set once the workflow fails.
    pub failure: Option<String>,
}

impl WorkflowState {
    /// The empty state a replay starts from, before folding any events.
    pub fn initial() -> Self {
        Self {
            status: ExecutionStatus::Running,
            next_event_id: 1,
            pending_activities: BTreeMap::new(),
            started_timers: BTreeMap::new(),
            result: None,
            failure: None,
        }
    }

    pub fn is_terminal(&self) -> bool {
        self.status != ExecutionStatus::Running
    }
}

impl Default for WorkflowState {
    fn default() -> Self {
        Self::initial()
    }
}

/// Whether a queued task drives the workflow function or an activity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TaskKind {
    Workflow,
    Activity,
}

/// The opaque token a poll hands out and a respond hands back. It ties a response to
/// exactly one dispatched task: the run, whether it's a workflow or activity task, and
/// which scheduled event it corresponds to. Encoded as JSON bytes on the wire — the
/// worker never inspects it, so the shape is ours to evolve.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskToken {
    pub run_id: RunId,
    pub kind: TaskKind,
    pub scheduled_event_id: i64,
}

impl TaskToken {
    pub fn new(run_id: RunId, kind: TaskKind, scheduled_event_id: i64) -> Self {
        Self {
            run_id,
            kind,
            scheduled_event_id,
        }
    }

    /// Serialize for the `task_token` wire field. Infallible in practice (plain data).
    pub fn encode(&self) -> Vec<u8> {
        serde_json::to_vec(self).unwrap_or_default()
    }

    /// Parse a token off the wire; `None` if it's malformed or empty (a timed-out poll).
    pub fn decode(bytes: &[u8]) -> Option<Self> {
        serde_json::from_slice(bytes).ok()
    }
}

/// A workflow task handed to a worker: the history to replay (V2) or the sticky
/// delta (V5), plus the token to answer with.
#[derive(Debug, Clone)]
pub struct WorkflowTask {
    pub token: TaskToken,
    pub workflow_id: WorkflowId,
    pub run_id: RunId,
    pub history: Vec<Event>,
    pub sticky_cache_hit: bool,
}

/// An activity task handed to a worker: what to run and with what input.
#[derive(Debug, Clone)]
pub struct ActivityTask {
    pub token: TaskToken,
    pub workflow_id: WorkflowId,
    pub run_id: RunId,
    pub activity_type: String,
    pub input: Vec<u8>,
}

/// Milliseconds since the Unix epoch — the engine's one clock for event timestamps.
///
/// NOTE (V2): workflow *code* must never read the wall clock directly — that would
/// make replay non-deterministic. This helper is for the *server* stamping events, and
/// for timers; a workflow that needs "now" gets it from a recorded event, not here.
pub fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
