//! V2 — Autoscaling transcode worker pool. `src/workers.rs`.
//!
//! Transcoding an ABR ladder is the CPU-heavy part of the pipeline, and load is *bursty*:
//! one popular streamer going live can 10× the work in seconds. This is the vertical where
//! you do **real k8s ops** — the worker Deployment scales on demand, and the two things that
//! make that safe are yours to build:
//!
//! 1. **The autoscaler signal.** HPA can't see "transcode backlog" on its own. You expose the
//!    *queue depth per worker* as a metric (scraped into a custom/external metric HPA reads),
//!    so replicas track backlog instead of CPU alone — [`WorkerPool::desired_replicas`] is the
//!    shape of that signal.
//! 2. **At-least-once work leasing under pod churn.** When HPA scales *down* (or a node
//!    preempts a pod), an in-flight job must not be lost or done twice. A worker *claims* a job
//!    with a visibility-timeout **lease**; if its pod dies mid-transcode the lease expires and
//!    another worker retries — but a `complete` inside the lease acks it exactly once. This is
//!    the same claim/lease idea as the job queue (project 04), now backing a real Deployment
//!    with a PodDisruptionBudget + graceful drain.
//!
//! The durable queue is NATS JetStream (project 05's log). Scaffold state: the pool is
//! constructed and its queue-depth gauge reads; enqueue/claim/complete + the autoscale signal
//! are the V2 `todo!()` worklist.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_nats::jetstream;
use serde::{Deserialize, Serialize};

use crate::error::Result;

/// One unit of transcode work: turn the source of `stream_key` into one ABR rung.
/// A stream fans out into one job per ladder rung; a worker claims and produces the
/// packaged segments for that rendition.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TranscodeJob {
    pub id: String,
    pub stream_key: String,
    /// Which ladder rung this job produces, e.g. `"720p"`.
    pub rendition: String,
    /// Unix-millis the job was enqueued (for queue-wait latency + lease math).
    pub enqueued_at_ms: i64,
}

/// Config for the pool, read from env in `main`.
pub struct WorkerConfig {
    /// JetStream stream name backing the transcode queue.
    pub stream_name: String,
    /// Visibility-timeout lease: how long a claimed job stays invisible before it's
    /// assumed abandoned (worker pod died) and redelivered.
    pub lease: Duration,
    /// The backlog-per-replica the autoscaler targets — the knob that decides how
    /// aggressively `desired_replicas` grows with queue depth.
    pub target_backlog_per_worker: usize,
    /// Replica ceiling the pool will ask HPA to scale to.
    pub max_replicas: usize,
}

/// The transcode worker pool's control surface. Workers themselves are separate k8s
/// pods; this type is what *enqueues* jobs (from the control plane on `→ Transcoding`),
/// exposes the autoscaler signal, and — inside a worker — claims/acks jobs.
pub struct WorkerPool {
    cfg: WorkerConfig,
    js: jetstream::Context,
    /// Best-effort gauge of jobs waiting (not yet claimed). The source of truth is the
    /// JetStream consumer's pending count; this mirrors it for the hot `/status` +
    /// autoscale reads without a round-trip.
    queue_depth: Arc<AtomicUsize>,
}

impl WorkerPool {
    /// Build the pool over a JetStream context. Wiring only.
    pub fn new(cfg: WorkerConfig, js: jetstream::Context) -> Self {
        Self {
            cfg,
            js,
            queue_depth: Arc::new(AtomicUsize::new(0)),
        }
    }

    pub fn config(&self) -> &WorkerConfig {
        &self.cfg
    }

    /// Current best-effort backlog (waiting jobs). Feeds a `/status` + `/metrics` gauge.
    pub fn queue_depth(&self) -> usize {
        self.queue_depth.load(Ordering::Relaxed)
    }

    // ---- V2 worklist: the queue, the lease, and the autoscale signal --------------

    /// TODO(V2): Ensure the JetStream stream/consumer backing the transcode queue
    /// exists (idempotent). Called once from `main` before serving.
    pub async fn ensure_queue(&self) -> Result<()> {
        let _ = &self.js;
        todo!("V2: create-or-get the JetStream stream + durable consumer for transcode jobs")
    }

    /// TODO(V2): Publish a transcode job onto the durable queue (called by the control
    /// plane, one per ladder rung, when a stream enters `Transcoding`). Bump the depth gauge.
    pub async fn enqueue(&self, job: TranscodeJob) -> Result<()> {
        let _ = job;
        todo!("V2: publish the job to JetStream, increment queue_depth")
    }

    /// TODO(V2): A worker asks for the next job. Claim one with a visibility-timeout
    /// **lease** so a peer can't also run it; return `None` when the queue is empty.
    /// If the worker's pod dies before [`complete`](Self::complete), the lease expires
    /// and the job is redelivered — at-least-once, never lost.
    pub async fn claim(&self, worker_id: &str) -> Result<Option<TranscodeJob>> {
        let _ = worker_id;
        todo!("V2: pull one message under a lease/visibility timeout, decrement queue_depth")
    }

    /// TODO(V2): Ack a finished job inside its lease so it's removed exactly once. A
    /// double-`complete` (redelivery raced a slow ack) must be harmless.
    pub async fn complete(&self, job_id: &str) -> Result<()> {
        let _ = job_id;
        todo!("V2: ack the JetStream message so it isn't redelivered")
    }

    /// TODO(V2): The autoscaler signal HPA consumes. Turn current backlog into a desired
    /// replica count — `ceil(queue_depth / target_backlog_per_worker)`, clamped to
    /// `[1, max_replicas]`. Exported as a metric so a custom/external-metric HPA scales the
    /// worker Deployment on *backlog*, not just CPU. This is the number the boss fight watches.
    pub fn desired_replicas(&self) -> usize {
        todo!("V2: derive desired replicas from queue depth, clamp to [1, max_replicas]")
    }
}
