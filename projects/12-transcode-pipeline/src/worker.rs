//! V3 — Parallel transcode workers: run the DAG's tasks, idempotently.
//!
//! A worker is a loop: **claim** a `Ready` task (leased, `SKIP LOCKED`), **run** it,
//! **settle** it (`complete` on success, `fail` → retry/dead-letter on error), then
//! go again. Run several and the DAG's fan-out becomes real parallelism — dozens of
//! chunk transcodes in flight at once, one per worker.
//!
//! The loop below is wired. The learning is what a worker *does* and how safely:
//!   * **`Split`** — probe the source, plan chunks (V1), and expand the DAG (V2).
//!   * **`Transcode`** — the crux of V3: shell to ffmpeg to transcode *exactly one
//!     chunk* at one rendition, **idempotently**. Because a lease can expire and a
//!     task re-run (at-least-once), a re-run must reproduce the same chunk bytes and
//!     commit them atomically (write-temp-then-rename), so a duplicate run is
//!     harmless and a half-written file is never mistaken for done.
//!   * **`Stitch`** — hand off to V4.
//!
//! Workers run only when `RUN_WORKERS=true` (see `main.rs`); with the store methods
//! still `todo!()`, a worker panics on its first `claim_ready`. That panic is the
//! V3 worklist.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::error::AppError;
use crate::job::{JobStore, PipelineConfig, Task, TaskKind};
use crate::{chunk, dag, ffmpeg, stitch};

/// Per-worker tuning, cloned into each spawned worker.
#[derive(Debug, Clone)]
pub struct WorkerConfig {
    /// How long an idle worker waits before polling for a `Ready` task again.
    pub poll_interval: Duration,
    /// Lease length stamped on each claimed task; the reaper reclaims it if the
    /// worker doesn't finish (or renew) within this window.
    pub lease: Duration,
}

/// One worker: an id plus shared handles to the store, the pipeline config, and
/// its tuning.
pub struct Worker {
    id: String,
    store: Arc<JobStore>,
    cfg: Arc<PipelineConfig>,
    wcfg: WorkerConfig,
}

impl Worker {
    pub fn new(
        id: String,
        store: Arc<JobStore>,
        cfg: Arc<PipelineConfig>,
        wcfg: WorkerConfig,
    ) -> Self {
        Self {
            id,
            store,
            cfg,
            wcfg,
        }
    }

    /// Drain the DAG until shutdown: claim → run → settle → repeat.
    pub async fn run(self, mut shutdown: watch::Receiver<bool>) {
        info!(worker = %self.id, "worker started");
        loop {
            if *shutdown.borrow() {
                break;
            }

            // V3: claim one Ready task, leased to this worker.
            let claimed = match self.store.claim_ready(&self.id, self.wcfg.lease).await {
                Ok(task) => task,
                Err(e) => {
                    error!(worker = %self.id, error = %e, "claim failed");
                    None
                }
            };

            let Some(task) = claimed else {
                // Nothing ready — wait, but stay responsive to shutdown.
                tokio::select! {
                    _ = tokio::time::sleep(self.wcfg.poll_interval) => {}
                    _ = shutdown.changed() => break,
                }
                continue;
            };

            self.process(task).await;
        }
        info!(worker = %self.id, "worker stopped");
    }

    /// Run a single claimed task and settle it.
    async fn process(&self, task: Task) {
        let id = task.id;
        debug!(worker = %self.id, task = %id, attempt = task.attempts, kind = ?task.kind, "running task");

        match self.execute(&task).await {
            Ok(()) => {
                if let Err(e) = self.store.complete(id).await {
                    error!(task = %id, error = %e, "complete failed");
                }
            }
            Err(err) => {
                // V3: retry-with-backoff or dead-letter, based on attempts.
                warn!(task = %id, error = %err, "task failed");
                if let Err(e) = self
                    .store
                    .fail(id, &err.to_string(), self.cfg.max_attempts)
                    .await
                {
                    error!(task = %id, error = %e, "settling failed task failed");
                }
            }
        }
    }

    /// Dispatch a task to its handler by kind. The `Split` arm is wired down to the
    /// two vertical calls (V1 `plan_chunks`, V2 `expand`); `Transcode` and `Stitch`
    /// hand off to the `todo!()`s.
    async fn execute(&self, task: &Task) -> Result<(), AppError> {
        match &task.kind {
            TaskKind::Split => {
                // Probe (plumbing) → plan chunks (V1) → expand the DAG (V2).
                let (source, ladder) = self.store.job_context(task.job_id).await?;
                let src = self.cfg.resolve_source(&source)?;
                let src = src.to_string_lossy().into_owned();

                let keyframes = ffmpeg::probe_keyframes(&self.cfg.ffprobe_bin, &src).await?;
                let duration = ffmpeg::probe_duration(&self.cfg.ffprobe_bin, &src).await?;

                let chunks = chunk::plan_chunks(&keyframes, duration, self.cfg.target_chunk_secs);
                info!(task = %task.id, chunks = chunks.len(), renditions = ladder.len(), "planned chunks");

                let tasks = dag::expand(task.job_id, task.id, &chunks, &ladder);
                self.store.add_tasks(&tasks).await?;
                Ok(())
            }
            TaskKind::Transcode { chunk, rendition } => {
                self.transcode_chunk(task, *chunk, rendition).await
            }
            TaskKind::Stitch { rendition } => {
                let chunk_dir = self.cfg.chunk_dir(task.job_id, rendition);
                let out = self.cfg.rendition_output(task.job_id, rendition);
                stitch::stitch(&self.cfg.ffmpeg_bin, &chunk_dir, &out).await
            }
        }
    }

    /// Transcode one chunk at one rendition — the idempotent unit of parallel work.
    ///
    /// TODO(V3): build and run the ffmpeg command that transcodes just this chunk.
    ///   - Cut the source to this chunk's `[start, end)` (from the plan) and encode
    ///     it at `rendition` (scale to height, target bitrate). Use
    ///     `ffmpeg::run(&self.cfg.ffmpeg_bin, &args)`.
    ///   - Make it **deterministic**: fixed encoder settings, no wall-clock/random
    ///     metadata, so a re-run yields identical bytes.
    ///   - Write to a temp path, then atomically rename into
    ///     `self.cfg.chunk_dir(job, rendition)/<chunk index>.mp4` — so an
    ///     at-least-once re-run can't leave a half-file, and a completed chunk is
    ///     detectable and skippable.
    async fn transcode_chunk(
        &self,
        task: &Task,
        chunk: u32,
        rendition: &str,
    ) -> Result<(), AppError> {
        let _ = (task, chunk, rendition, &self.cfg.ffmpeg_bin);
        todo!("V3: deterministically transcode this one chunk, commit atomically (temp→rename)")
    }
}
