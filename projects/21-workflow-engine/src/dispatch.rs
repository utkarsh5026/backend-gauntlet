//! V4 — gRPC worker dispatch: task queues, long-poll, at-least-once delivery.
//!
//! Workers don't get *pushed* work; they **long-poll** for it. A worker calls
//! `PollWorkflowTask` and the call blocks until either a task is claimable or the poll
//! times out (return empty, poll again). This is how the engine matches idle workers to
//! ready work without a scheduler that has to know who's alive.
//!
//! The delivery guarantee is **at-least-once**, and that is deliberate: when a worker
//! claims a task it takes a *visibility-timeout lease*, not ownership. Complete the task
//! in time and the lease is released (the task row is deleted); crash before you do —
//! The Reaper — and the lease lapses, the task becomes claimable again, and another
//! worker replays the history and carries on. At-least-once + deterministic replay (V2)
//! + idempotent effects is what adds up to "durable execution".
//!
//! This module is also the server-side **orchestrator**: it turns the commands a worker
//! returns into history events and their side effects (schedule an activity task, start
//! a durable timer via [`TimerService`], complete the execution), and refreshes the
//! sticky pin (V5). It leans on [`HistoryStore`] (V1), [`replay`](crate::replay) (V2),
//! [`TimerService`] (V3), and [`StickyCache`](crate::sticky) (V5) — the glue that makes
//! them one engine.

use std::sync::Arc;
use std::time::Duration;

use sqlx::PgPool;

use crate::error::AppError;
use crate::history::{HistoryStore, StartOptions};
use crate::model::{ActivityTask, Command, RunId, TaskToken, WorkflowState, WorkflowTask};
use crate::sticky::StickyCache;
use crate::timers::TimerService;

/// Dispatch tunables (from env; see `.env.example`).
#[derive(Debug, Clone, Copy)]
pub struct DispatcherConfig {
    /// How long a poll blocks before returning "no work".
    pub long_poll_timeout: Duration,
    /// How long a claimed-but-uncompleted task stays invisible before it's assumed
    /// lost and requeued. This is the knob that trades crash-recovery latency against
    /// the risk of two workers running a slow task.
    pub visibility_timeout: Duration,
}

/// The task-queue engine + server-side orchestrator. Cheap to share behind an `Arc`.
pub struct Dispatcher {
    pool: PgPool,
    history: Arc<HistoryStore>,
    timers: Arc<TimerService>,
    sticky: Arc<StickyCache>,
    cfg: DispatcherConfig,
}

impl Dispatcher {
    pub fn new(
        pool: PgPool,
        history: Arc<HistoryStore>,
        timers: Arc<TimerService>,
        sticky: Arc<StickyCache>,
        cfg: DispatcherConfig,
    ) -> Arc<Self> {
        Arc::new(Self {
            pool,
            history,
            timers,
            sticky,
            cfg,
        })
    }

    /// Start a new execution and enqueue its first workflow task.
    ///
    /// TODO(V4): create the execution + `WORKFLOW_STARTED` via `self.history`
    /// ([`HistoryStore::start_execution`]), then INSERT a `kind='workflow'` row into
    /// `task_queue` for `opts.task_queue` so a worker can pick it up. Return the run id.
    pub async fn start_workflow(&self, opts: StartOptions) -> Result<RunId, AppError> {
        let _ = (&self.pool, &self.history, opts);
        todo!("V4: open a history and enqueue the first workflow task")
    }

    /// Long-poll for the next workflow task on `task_queue`. `Ok(None)` = timed out.
    ///
    /// TODO(V4/V5): claim a pending `kind='workflow'` task (FOR UPDATE SKIP LOCKED,
    /// ordered by `visible_at`), set its `visible_at = now + visibility_timeout` and
    /// `locked_by = identity`. Then build the [`WorkflowTask`]:
    ///   - sticky HIT: if `self.sticky.lookup(run)` pins this run to `identity`, return
    ///     only the events after the pin's `last_event_id` (via
    ///     [`HistoryStore::load_history_after`]) with `sticky_cache_hit = true`;
    ///   - sticky MISS: return the full history ([`HistoryStore::load_history`]) so the
    ///     worker replays from scratch ([`crate::replay::replay`]).
    /// Record `metrics::REPLAYS_TOTAL` with the right `sticky` label. Block up to
    /// `self.cfg.long_poll_timeout` when nothing is claimable, then return `Ok(None)`.
    pub async fn poll_workflow_task(
        &self,
        task_queue: &str,
        identity: &str,
    ) -> Result<Option<WorkflowTask>, AppError> {
        let _ = (
            &self.pool,
            &self.history,
            &self.sticky,
            self.cfg,
            task_queue,
            identity,
            crate::replay::replay,
            crate::metrics::REPLAYS_TOTAL,
        );
        todo!("V4/V5: long-poll + claim a workflow task; sticky-aware history slice")
    }

    /// Apply the commands a worker produced this workflow task.
    ///
    /// TODO(V2/V4/V5): this is the orchestrator's core. In one transaction:
    ///   1. Validate the worker didn't diverge from history
    ///      ([`crate::replay::check_determinism`]) — else `NonDeterministic`.
    ///   2. Turn each command into event(s) and the side effect it implies:
    ///        ScheduleActivity → append ACTIVITY_SCHEDULED + enqueue an activity task;
    ///        StartTimer       → append TIMER_STARTED + `self.timers.schedule_timer`;
    ///        Complete/Fail    → append WORKFLOW_COMPLETED/FAILED, mark the execution
    ///                           terminal, and `metrics::EXECUTIONS_COMPLETED_TOTAL`.
    ///   3. Append WORKFLOW_TASK_COMPLETED, delete the claimed task row, and refresh the
    ///      sticky pin for `identity` ([`StickyCache::pin`]) so its next task is sticky.
    /// The token proves which task/run this answers; reject a token whose task is no
    /// longer the active claim (a late worker whose lease already lapsed).
    pub async fn complete_workflow_task(
        &self,
        token: TaskToken,
        identity: &str,
        commands: Vec<Command>,
    ) -> Result<(), AppError> {
        let _ = (
            &self.pool,
            &self.history,
            &self.timers,
            &self.sticky,
            token,
            identity,
            commands,
            crate::replay::check_determinism,
            crate::metrics::EXECUTIONS_COMPLETED_TOTAL,
        );
        todo!("V4: apply a worker's commands to history + schedule their side effects")
    }

    /// Long-poll for the next activity task on `task_queue`. `Ok(None)` = timed out.
    ///
    /// TODO(V4): claim a pending `kind='activity'` task (SKIP LOCKED), lease it with the
    /// visibility timeout, and read its `activity_type` + input from the
    /// ACTIVITY_SCHEDULED event it points at. Count `metrics::ACTIVITY_TASKS_TOTAL`.
    pub async fn poll_activity_task(
        &self,
        task_queue: &str,
        identity: &str,
    ) -> Result<Option<ActivityTask>, AppError> {
        let _ = (
            &self.pool,
            &self.history,
            self.cfg,
            task_queue,
            identity,
            crate::metrics::ACTIVITY_TASKS_TOTAL,
        );
        todo!("V4: long-poll + claim an activity task")
    }

    /// Record an activity's successful result and wake its workflow.
    ///
    /// TODO(V4): append ACTIVITY_COMPLETED (with `result`) for the scheduled event the
    /// token names, delete the activity task row, and enqueue a workflow task so the
    /// workflow can react. All in one transaction.
    pub async fn complete_activity_task(
        &self,
        token: TaskToken,
        result: Vec<u8>,
    ) -> Result<(), AppError> {
        let _ = (&self.pool, &self.history, token, result);
        todo!("V4: record ACTIVITY_COMPLETED and enqueue the follow-up workflow task")
    }

    /// Record an activity failure and wake its workflow (which may retry or handle it).
    ///
    /// TODO(V4): append ACTIVITY_FAILED (with `failure`), delete the task row, enqueue a
    /// workflow task. A retry policy on top of this is a natural stretch.
    pub async fn fail_activity_task(
        &self,
        token: TaskToken,
        failure: String,
    ) -> Result<(), AppError> {
        let _ = (&self.pool, &self.history, token, failure);
        todo!("V4: record ACTIVITY_FAILED and enqueue the follow-up workflow task")
    }

    /// The execution's current state — terminal result when done, else "running".
    ///
    /// TODO(V4): load the history ([`HistoryStore::load_history`]) and fold it
    /// ([`crate::replay::replay`]); `NotFound` if the run id is unknown.
    pub async fn get_result(&self, run_id: RunId) -> Result<WorkflowState, AppError> {
        let _ = (&self.history, run_id, crate::replay::replay);
        todo!("V4: load + replay a run to report its current/terminal state")
    }
}
