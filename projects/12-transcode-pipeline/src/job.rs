//! The domain types + the durable DAG store.
//!
//! A **job** transcodes one source asset into a *ladder* of renditions. The work
//! is expressed as a **DAG of tasks** (V2): a `Split` task fans out into one
//! `Transcode` task per (chunk × rendition), and each rendition's `Stitch` task
//! fans those back in. The graph, its dependency edges, and each task's status +
//! lease all live in Postgres so a crashed worker's task becomes claimable again
//! and the whole pipeline survives a restart.
//!
//! The types here are concrete; the store methods are the V2/V3 `todo!()`s. Note
//! there are **no `sqlx::query!` macros yet** — that's why the scaffold compiles
//! offline. As you implement each method, add the compile-time-checked query
//! inside it (run `docker compose up -d` + `sqlx migrate run` first so the macros
//! can check against the live schema, or `cargo sqlx prepare` for the `.sqlx`
//! offline cache).

use std::path::{Path, PathBuf};
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;
use uuid::Uuid;

use crate::error::AppError;

/// Opaque, coordination-free ids (no shared sequence across workers/nodes).
pub type JobId = Uuid;
pub type TaskId = Uuid;

/// One rung of the output ABR ladder: a named quality target. The transcode task
/// (V3) turns this into deterministic encoder flags; the ladder is what makes the
/// fan-out wide (chunks × renditions).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rendition {
    /// Ladder rung name, also the output subdirectory (e.g. `720p`).
    pub name: String,
    /// Target height in pixels; width follows from the source aspect ratio.
    pub height: u32,
    /// Target video bitrate (kbps).
    pub v_bitrate_kbps: u32,
    /// Target audio bitrate (kbps).
    pub a_bitrate_kbps: u32,
}

/// Where a job/task sits in its lifecycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    /// Created but not yet runnable — waiting on upstream dependencies.
    Pending,
    /// All dependencies satisfied; claimable by a worker.
    Ready,
    /// Claimed and leased to a worker right now.
    Running,
    /// Finished successfully; its artifact exists.
    Done,
    /// Exhausted its retries — a dead task (blocks its downstream forever until
    /// requeued). A job with any failed task is itself failed.
    Failed,
}

/// What a task actually *does*. The dependency shape follows from the kind:
///   `Split` → many `Transcode` → per-rendition `Stitch`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum TaskKind {
    /// Probe the source, plan keyframe-aligned chunks (V1), and expand the DAG
    /// with the per-chunk transcode + per-rendition stitch tasks (V2).
    Split,
    /// Transcode exactly one chunk at one rendition (V3). Idempotent: re-running
    /// must reproduce the same chunk bytes.
    Transcode { chunk: u32, rendition: String },
    /// Concatenate + remux one rendition's chunks into the final output (V4).
    /// Depends on *every* `Transcode` for that rendition — the fan-in.
    Stitch { rendition: String },
}

/// A node in the job DAG.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: TaskId,
    pub job_id: JobId,
    pub kind: TaskKind,
    pub status: Status,
    /// Upstream tasks that must be `Done` before this one becomes `Ready`.
    pub deps: Vec<TaskId>,
    /// How many times this task has been attempted (for retry/backoff, V3).
    pub attempts: i32,
    /// When the current lease expires; past this a reaper reclaims a dead
    /// worker's task (V3). `None` when not `Running`.
    pub lease_until: Option<DateTime<Utc>>,
}

/// The request body of `POST /jobs`.
#[derive(Debug, Clone, Deserialize)]
pub struct NewJob {
    /// Path to the source file, resolved under `WORK_DIR` (never an arbitrary
    /// absolute path — validate this before use).
    pub source: String,
    /// Output ladder; empty means "use the server default ladder".
    #[serde(default)]
    pub ladder: Vec<Rendition>,
}

/// A read view of a job for the inspect endpoint.
#[derive(Debug, Clone, Serialize)]
pub struct JobView {
    pub id: JobId,
    pub source: String,
    pub ladder: Vec<Rendition>,
    pub status: Status,
    pub created_at: DateTime<Utc>,
    /// Per-status task counts, so a caller can watch the DAG drain
    /// (e.g. `{"done": 40, "running": 4, "ready": 12, "pending": 8}`).
    pub tasks: TaskCounts,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct TaskCounts {
    pub pending: i64,
    pub ready: i64,
    pub running: i64,
    pub done: i64,
    pub failed: i64,
}

/// Server-wide pipeline configuration, shared into every handler and worker.
#[derive(Debug, Clone)]
pub struct PipelineConfig {
    /// Root of the artifact store. Sources, chunk outputs, and finished
    /// renditions all live under here; nothing escapes it.
    pub work_dir: PathBuf,
    pub ffmpeg_bin: String,
    pub ffprobe_bin: String,
    /// Desired chunk length (seconds). A goal, not a law — the keyframe boundary
    /// wins (V1), so real chunks cluster around it.
    pub target_chunk_secs: f64,
    /// The ladder used when a job doesn't specify one.
    pub default_ladder: Vec<Rendition>,
    /// Retry ceiling before a task is dead-lettered (V3).
    pub max_attempts: i32,
}

impl PipelineConfig {
    /// Per-job scratch/artifact root: `WORK_DIR/jobs/<job_id>`.
    pub fn job_dir(&self, job: JobId) -> PathBuf {
        self.work_dir.join("jobs").join(job.to_string())
    }

    /// Directory holding one rendition's transcoded chunk files, before stitching:
    /// `.../<job_id>/<rendition>/chunks`.
    pub fn chunk_dir(&self, job: JobId, rendition: &str) -> PathBuf {
        self.job_dir(job).join(rendition).join("chunks")
    }

    /// The final stitched output for a rendition: `.../<job_id>/<rendition>/out.mp4`.
    pub fn rendition_output(&self, job: JobId, rendition: &str) -> PathBuf {
        self.job_dir(job).join(rendition).join("out.mp4")
    }

    /// Resolve + validate a client-supplied source path so it can never escape
    /// `WORK_DIR` (`..`, absolute paths, symlinks out).
    ///
    /// TODO(security): implement the traversal guard here (canonicalize under
    /// `work_dir` and reject anything that resolves outside it). Left as a guard
    /// stub so the wiring type-checks; see the SPEC security checklist.
    pub fn resolve_source(&self, source: &str) -> Result<PathBuf, AppError> {
        let _ = source;
        todo!("security: resolve `source` under WORK_DIR without allowing traversal")
    }
}

/// The durable DAG store: every method is a Postgres round-trip against the
/// `jobs` / `tasks` / `task_deps` schema in `migrations/`.
pub struct JobStore {
    pool: PgPool,
}

impl JobStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// The underlying pool, for the parts of V2/V3 that want a transaction.
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Create a job and seed its DAG with a single `Ready` `Split` task.
    ///
    /// TODO(V2): in one transaction, insert the `jobs` row (source + ladder) and
    /// the seed `Split` task (status `Ready`, no deps). The rest of the graph is
    /// discovered later, when a worker runs `Split` and calls `add_tasks`.
    pub async fn submit(&self, new: &NewJob, ladder: &[Rendition]) -> Result<JobId, AppError> {
        let _ = (new, ladder);
        todo!("V2: insert job + seed Split task (Ready), return the new job id")
    }

    /// Full read view of a job + its per-status task counts.
    ///
    /// TODO(V2): join `jobs` with an aggregate over `tasks`.
    pub async fn get_job(&self, id: JobId) -> Result<Option<JobView>, AppError> {
        let _ = id;
        todo!("V2: load the job row + GROUP BY status task counts")
    }

    /// The source path + ladder for a job — what a worker needs to run `Split`.
    ///
    /// TODO(V2): single-row lookup on `jobs`.
    pub async fn job_context(&self, id: JobId) -> Result<(String, Vec<Rendition>), AppError> {
        let _ = id;
        todo!("V2: return (source, ladder) for the job")
    }

    /// Persist a batch of newly-discovered tasks + their dependency edges (the
    /// `Split` expansion). New transcode tasks start `Pending`; the scheduler
    /// promotes them once their (already-`Done` split) deps are satisfied.
    ///
    /// TODO(V2): bulk-insert the `tasks` rows and the `task_deps` edges atomically.
    pub async fn add_tasks(&self, tasks: &[Task]) -> Result<(), AppError> {
        let _ = tasks;
        todo!("V2: bulk-insert tasks + their dependency edges in one transaction")
    }

    /// Atomically claim one `Ready` task for `worker`, flipping it to `Running`
    /// with a fresh lease. Returns `None` when nothing is ready.
    ///
    /// TODO(V3): this is the classic `FOR UPDATE SKIP LOCKED` claim (same idea as
    /// project 04). Pick one `Ready` task, set `status=Running`, bump `attempts`,
    /// stamp `lease_until = now() + lease`, and return it.
    pub async fn claim_ready(
        &self,
        worker: &str,
        lease: Duration,
    ) -> Result<Option<Task>, AppError> {
        let _ = (worker, lease);
        todo!("V3: claim one Ready task (FOR UPDATE SKIP LOCKED), lease it, return it")
    }

    /// Mark a task `Done` after its artifact is committed. This is what unblocks
    /// downstream tasks (the scheduler notices on its next pass).
    ///
    /// TODO(V3): set `status=Done`, clear the lease.
    pub async fn complete(&self, id: TaskId) -> Result<(), AppError> {
        let _ = id;
        todo!("V3: mark the task Done and clear its lease")
    }

    /// Settle a failed attempt: back to `Ready` for a retry if attempts remain,
    /// else `Failed` (which also fails the job).
    ///
    /// TODO(V3): compare `attempts` to `max_attempts`; requeue or dead-letter.
    pub async fn fail(&self, id: TaskId, err: &str, max_attempts: i32) -> Result<(), AppError> {
        let _ = (id, err, max_attempts);
        todo!("V3: retry-or-dead-letter based on attempts vs max_attempts")
    }

    /// Scheduler pass: promote every `Pending` task whose deps are all `Done` to
    /// `Ready`. Returns how many it promoted.
    ///
    /// TODO(V2): the DAG readiness query — a `Pending` task with zero remaining
    /// `task_deps` pointing at a non-`Done` task becomes `Ready`. (The pure,
    /// in-memory twin of this is `dag::newly_ready`, which the SPEC has you
    /// property-test.)
    pub async fn promote_ready(&self) -> Result<u64, AppError> {
        todo!("V2: promote Pending tasks whose dependencies are all Done")
    }

    /// Reaper pass: reclaim `Running` tasks whose lease has expired (their worker
    /// died) back to `Ready`. Returns how many it reclaimed.
    ///
    /// TODO(V3): set `status=Ready` where `status=Running AND lease_until < now()`.
    pub async fn reclaim_expired(&self) -> Result<u64, AppError> {
        todo!("V3: reclaim Running tasks whose lease has expired back to Ready")
    }
}

/// Number of transcoded chunk files present for a rendition — used by the stitch
/// task to know its inputs. Wired helper (a directory count), not a vertical.
pub fn count_chunk_files(dir: &Path) -> std::io::Result<usize> {
    let mut n = 0;
    match std::fs::read_dir(dir) {
        Ok(entries) => {
            for e in entries {
                let e = e?;
                if e.file_type()?.is_file() {
                    n += 1;
                }
            }
            Ok(n)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Err(e) => Err(e),
    }
}
