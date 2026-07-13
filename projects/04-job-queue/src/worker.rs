//! The worker runtime: the loop that drains the queue.
//!
//! This is wiring — it *calls into* the verticals (claim = V1, lease via the
//! reaper = V2, retry/DLQ = V3, wakeup = V4) and ties them into a lifecycle:
//! claim a batch → run each job → ack on success / nack on failure → repeat.
//! With the verticals still `todo!()`, a worker panics on its first `claim`;
//! that panic message is the V1 worklist. Workers run only when `RUN_WORKERS=true`
//! (see `main.rs`), so the bare scaffold serves the enqueue API cleanly.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::job::Job;
use crate::queue::Queue;
use crate::retry::{self, Disposition, RetryPolicy};

/// Per-worker tuning, cloned into each spawned worker.
#[derive(Debug, Clone)]
pub struct WorkerConfig {
    /// Which named queue this worker drains.
    pub queue_name: String,
    /// How long an idle worker waits before re-checking (V4 replaces this with a
    /// LISTEN/NOTIFY wakeup — see `scheduler::wait_for_work`).
    pub poll_interval: Duration,
    /// Lease length stamped on each claimed job (V2).
    pub visibility_timeout: Duration,
    /// How many jobs to claim per round-trip.
    pub claim_batch: i64,
    /// Backoff policy for failed jobs (V3).
    pub retry: RetryPolicy,
}

pub async fn run(
    id: String,
    queue: Arc<Queue>,
    cfg: WorkerConfig,
    mut shutdown: watch::Receiver<bool>,
) {
    info!(worker = %id, queue = %cfg.queue_name, "worker started");

    loop {
        let queue_name = &cfg.queue_name;
        let claimed = match queue
            .claim(queue_name, &id, cfg.claim_batch, cfg.visibility_timeout)
            .await
        {
            Ok(jobs) => jobs,
            Err(e) => {
                error!(worker = %id, error = %e, "claim failed");
                Vec::new()
            }
        };

        if claimed.is_empty() {
            // Nothing to do — wait, but stay responsive to shutdown.
            // TODO(V4): replace this fixed sleep with `scheduler::wait_for_work`
            // so an enqueue NOTIFY wakes an idle worker immediately instead of
            // adding up to `poll_interval` of latency to every job.
            tokio::select! {
                _ = tokio::time::sleep(cfg.poll_interval) => {}
                _ = shutdown.changed() => break,
            }
            continue;
        }

        for job in claimed {
            if *shutdown.borrow() {
                break;
            }
            process_one(&queue, &cfg, &id, job).await;
        }

        if *shutdown.borrow() {
            break;
        }
    }

    info!(worker = %id, "worker stopped");
}

/// One span per job attempt. `skip_all` keeps the queue/cfg/job args out of the
/// span (no payload leakage — see SPEC security note); the explicit `fields` carry
/// the SPEC observability trio (`job.id`, `kind`, `attempt`) plus `worker`, and
/// `outcome`/`elapsed_ms` are recorded once the job resolves so the span itself is
/// queryable ("show me dead-lettered jobs", "p99 of elapsed_ms").
#[tracing::instrument(
    skip_all,
    fields(
        worker = %worker,
        job.id = job.id,
        kind = %job.kind,
        attempt = job.attempts,
        outcome = tracing::field::Empty,
        elapsed_ms = tracing::field::Empty,
    )
)]
async fn process_one(queue: &Arc<Queue>, cfg: &WorkerConfig, worker: &str, job: Job) {
    let id = job.id;
    let span = tracing::Span::current();
    debug!("processing job");

    let started = Instant::now();
    let result = crate::handlers::dispatch(&job).await;
    let elapsed_ms = started.elapsed().as_millis() as u64;
    span.record("elapsed_ms", elapsed_ms);

    match result {
        Ok(()) => match queue.ack(id).await {
            Ok(()) => {
                span.record("outcome", "done");
                info!(elapsed_ms, "job done");
            }
            Err(e) => {
                span.record("outcome", "ack_failed");
                error!(error = %e, "ack failed");
            }
        },
        Err(err) => match retry::nack(queue.pool(), &cfg.retry, &job, &err).await {
            Ok(Disposition::Retried) => {
                span.record("outcome", "retried");
                warn!(error = %err, "job failed; scheduled for retry");
            }
            Ok(Disposition::DeadLettered) => {
                span.record("outcome", "dead_lettered");
                error!(error = %err, "job exhausted retries; dead-lettered");
            }
            Err(e) => {
                span.record("outcome", "nack_failed");
                error!(error = %e, "nack failed");
            }
        },
    }
}
