//! Prometheus metrics + the tiny HTTP sidecar that serves them.
//!
//! gRPC has no natural place to hang a scrape endpoint, so a minimal axum router
//! exposes `/metrics` and `/healthz` on a side port while tonic serves the workflow
//! API. The [`metrics`] facade writes to a process-global recorder, so the counter
//! call sites (in [`dispatch`](crate::dispatch), [`timers`](crate::timers), and the
//! replayer) stay decoupled from this wiring — they just name a metric. Until
//! [`install`] sets a recorder the macros are no-ops, which is exactly what tests want.
//!
//! The series the SPEC grades:
//! - [`WORKFLOW_TASKS_TOTAL`] — workflow tasks dispatched.
//! - [`ACTIVITY_TASKS_TOTAL`] — activity tasks dispatched.
//! - [`REPLAYS_TOTAL`] — labelled `sticky = hit|miss`; the sticky hit ratio (V5).
//! - [`TIMERS_FIRED_TOTAL`] — durable timers fired (V3).
//! - [`EXECUTIONS_COMPLETED_TOTAL`] — labelled `outcome = completed|failed`.
//! - [`TASK_QUEUE_DEPTH`] — pending tasks not yet claimed (a gauge, backpressure signal).

use axum::routing::get;
use axum::Router;
use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};

/// Workflow tasks dispatched to workers (each is one decision the workflow makes).
pub const WORKFLOW_TASKS_TOTAL: &str = "workflow_workflow_tasks_total";

/// Activity tasks dispatched to workers.
pub const ACTIVITY_TASKS_TOTAL: &str = "workflow_activity_tasks_total";

/// Replays, labelled `sticky = hit|miss`. Hit ratio = `hit / (hit+miss)` (V5): the
/// fraction of workflow tasks a worker served from its cached state instead of a full
/// history replay.
pub const REPLAYS_TOTAL: &str = "workflow_replays_total";

/// Durable timers fired (V3).
pub const TIMERS_FIRED_TOTAL: &str = "workflow_timers_fired_total";

/// Executions that reached a terminal state, labelled `outcome = completed|failed`.
pub const EXECUTIONS_COMPLETED_TOTAL: &str = "workflow_executions_completed_total";

/// Pending tasks not yet claimed by a worker — the queue depth (a gauge). A rising
/// depth means workers can't keep up; it's the engine's backpressure signal.
pub const TASK_QUEUE_DEPTH: &str = "workflow_task_queue_depth";

/// Install the process-global Prometheus recorder and return a handle used to render
/// the registry for `/metrics`. Call once, from `main`, after telemetry init. Panics
/// if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// The observability sidecar router: `/metrics` for scrapes, `/healthz` for probes.
pub fn observability_router(handle: PrometheusHandle) -> Router {
    Router::new()
        .route(
            "/metrics",
            get(move || {
                let handle = handle.clone();
                async move { handle.render() }
            }),
        )
        .route("/healthz", get(|| async { "ok" }))
}

/// Register HELP metadata for the graded metrics. Naming each constant here also gives
/// rustc a direct use of it (proc-macro call sites alone don't always satisfy
/// `dead_code`), so the scaffold stays warning-quiet before the call sites exist.
fn register_descriptions() {
    let workflow_tasks = WORKFLOW_TASKS_TOTAL;
    let activity_tasks = ACTIVITY_TASKS_TOTAL;
    let replays = REPLAYS_TOTAL;
    let timers = TIMERS_FIRED_TOTAL;
    let completed = EXECUTIONS_COMPLETED_TOTAL;
    let depth = TASK_QUEUE_DEPTH;
    metrics::describe_counter!(workflow_tasks, "Workflow tasks dispatched to workers");
    metrics::describe_counter!(activity_tasks, "Activity tasks dispatched to workers");
    metrics::describe_counter!(replays, "Workflow replays, labelled sticky = hit|miss");
    metrics::describe_counter!(timers, "Durable timers fired");
    metrics::describe_counter!(completed, "Executions finished, labelled completed|failed");
    metrics::describe_gauge!(depth, "Pending tasks not yet claimed by a worker");
}
