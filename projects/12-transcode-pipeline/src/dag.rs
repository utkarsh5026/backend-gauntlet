//! V2 — The job DAG: model the pipeline as a dependency graph and schedule it.
//!
//! A transcode job isn't a queue of independent jobs — it's a graph with a shape:
//!
//! ```text
//!                    ┌── transcode(chunk 0, 720p) ─┐
//!                    ├── transcode(chunk 1, 720p) ─┤
//!   split ──fan-out──┼── transcode(chunk 2, 720p) ─┼─fan-in─→ stitch(720p)
//!                    ├── transcode(chunk 0, 480p) ─┤
//!                    └──          …                ┴─fan-in─→ stitch(480p)
//! ```
//!
//! Two jobs live here:
//!   * **`expand`** — build the transcode + stitch tasks (and their edges) once the
//!     `Split` task has discovered the chunk count. This is where the DAG's *shape*
//!     is defined: every `Transcode` depends on `Split`; every `Stitch` depends on
//!     *all* of its rendition's `Transcode`s (the fan-in that the boss exploits).
//!   * **`newly_ready`** — the scheduler's core: given the current task states,
//!     which `Pending` tasks now have *all* dependencies `Done` and so become
//!     runnable? This is the pure, in-memory twin of `JobStore::promote_ready`, and
//!     it's what you property-test.
//!
//! `schedule_loop` is the wired background pass that drives the store forward; the
//! two functions above are the `todo!()`s.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tracing::{debug, error, info};

use crate::chunk::ChunkPlan;
use crate::job::{JobId, JobStore, Rendition, Status, Task, TaskId};

/// Build every downstream task for a job once its chunks are known.
///
/// `split_task` is the already-running `Split` task the transcodes depend on;
/// `chunks` are V1's plan; `ladder` is the output ladder.
///
/// TODO(V2): construct the graph.
///   - For each rendition × each chunk, make a `Transcode { chunk, rendition }`
///     task that depends on `split_task` (status `Pending`).
///   - For each rendition, make one `Stitch { rendition }` task that depends on
///     **every** `Transcode` of that rendition — the fan-in join.
///   - Fresh `TaskId`s; `attempts = 0`; `lease_until = None`.
/// The returned tasks are what `JobStore::add_tasks` persists.
pub fn expand(
    job_id: JobId,
    split_task: TaskId,
    chunks: &[ChunkPlan],
    ladder: &[Rendition],
) -> Vec<Task> {
    let _ = (job_id, split_task, chunks, ladder);
    todo!("V2: build transcode tasks (dep: split) + per-rendition stitch tasks (dep: all their transcodes)")
}

/// The scheduler's readiness rule: which `Pending` tasks are now runnable?
///
/// A task becomes ready exactly when **all** of its dependencies are `Done`. A
/// task with no dependencies is ready immediately.
///
/// TODO(V2): return the ids of every `Pending` task in `tasks` whose `deps` all
/// resolve to a `Done` task in the same slice. Keep it pure (no I/O) — it's the
/// testable heart of the scheduler and the mirror of `JobStore::promote_ready`.
pub fn newly_ready(tasks: &[Task]) -> Vec<TaskId> {
    let _ = tasks;
    todo!("V2: return Pending tasks whose every dependency is Done")
}

/// Background scheduler + reaper: on a fixed tick, promote newly-ready tasks and
/// reclaim tasks whose worker lease expired. Wired for you — it just calls the
/// store methods you implement (`promote_ready` = V2, `reclaim_expired` = V3), so
/// with those still `todo!()` it panics on the first tick once `RUN_WORKERS=true`.
pub async fn schedule_loop(
    store: Arc<JobStore>,
    interval: Duration,
    mut shutdown: watch::Receiver<bool>,
) {
    info!(?interval, "scheduler started");
    loop {
        tokio::select! {
            _ = tokio::time::sleep(interval) => {}
            _ = shutdown.changed() => break,
        }
        if *shutdown.borrow() {
            break;
        }

        // V2: Pending → Ready when deps are Done. This is what makes progress
        // flow along the DAG as transcodes finish.
        match store.promote_ready().await {
            Ok(n) if n > 0 => debug!(promoted = n, "tasks became ready"),
            Ok(_) => {}
            Err(e) => error!(error = %e, "promote_ready failed"),
        }

        // V3: reclaim tasks a dead worker was holding — the reaper half of the
        // lease. Without this, one crashed worker strands its chunk and the
        // fan-in stitch waits forever (the boss's straggler).
        match store.reclaim_expired().await {
            Ok(n) if n > 0 => debug!(reclaimed = n, "expired leases reclaimed"),
            Ok(_) => {}
            Err(e) => error!(error = %e, "reclaim_expired failed"),
        }
    }
    info!("scheduler stopped");
}

/// Convenience wrapper the tests can lean on: a task is runnable iff pending and
/// all deps done. Kept tiny and total so `newly_ready` has an obvious spec.
pub fn deps_all_done(task: &Task, done: &dyn Fn(TaskId) -> bool) -> bool {
    task.status == Status::Pending && task.deps.iter().all(|d| done(*d))
}
