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

use chrono::Utc;
use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::job::Job;
use crate::queue::Queue;
use crate::retry::{self, Disposition, RetryPolicy};
use crate::scheduler;

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

/// Drive one worker: claim → run → ack/nack in a loop until shutdown.
///
/// Each iteration claims a batch of up to [`WorkerConfig::claim_batch`] jobs (V1)
/// and hands each to [`process_one`]. When a claim comes back empty the worker
/// parks on [`scheduler::wait_for_work`] — a `LISTEN`/`NOTIFY` wakeup bounded by
/// [`WorkerConfig::poll_interval`] (V4) — until work arrives or `shutdown` flips.
///
/// `id` labels the worker in logs and spans; `queue` is shared across all workers
/// (hence `Arc`), and `shutdown` is a broadcast flag — flipping it to `true` makes
/// the worker finish its in-flight job and return (it re-checks between jobs, so a
/// mid-batch shutdown drains at most one more job). Claim and `wait_for_work`
/// errors are logged and retried on the next tick rather than bubbling up, so a
/// transient DB hiccup doesn't kill the worker. Returns only once `shutdown` is
/// observed.
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
            tokio::select! {
                result = scheduler::wait_for_work(queue.pool(), queue_name, cfg.poll_interval) => {
                    if let Err(e) = result {
                        error!(worker = %id, error = %e, "wait_for_work failed");
                    }
                }
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

    metrics::histogram!(crate::metrics::EXECUTION_SECONDS, "kind" => job.kind.clone())
        .record(started.elapsed().as_secs_f64());

    match result {
        Ok(()) => match queue.ack(id).await {
            Ok(()) => {
                span.record("outcome", "done");
                info!(elapsed_ms, "job done");
                metrics::counter!(crate::metrics::COMPLETED_TOTAL, "kind" => job.kind.clone())
                    .increment(1);
                let e2e = (Utc::now() - job.created_at).num_milliseconds().max(0) as f64 / 1000.0;
                metrics::histogram!(crate::metrics::END_TO_END_LATENCY_SECONDS, "kind" => job.kind.clone())
                    .record(e2e);
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

#[cfg(test)]
mod tests {
    //! Worker-loop tests. `#[sqlx::test]` hands each test its own freshly-migrated
    //! database (matching `queue`/`lease`/`retry`), so a spawned worker + its
    //! `LISTEN`/`NOTIFY` traffic never collide with a parallel test.
    use super::*;

    use crate::job::{JobState, NewJob};
    use crate::queue::Queue;

    use sqlx::PgPool;
    use tokio::sync::watch;

    /// A ready-when-`delay_secs`-elapses `noop` job (succeeds immediately once run).
    fn noop_job(queue: &str, delay_secs: i64) -> NewJob {
        NewJob {
            queue: queue.to_string(),
            kind: "noop".into(),
            payload: serde_json::Value::Null,
            max_attempts: None,
            delay_secs: Some(delay_secs),
        }
    }

    /// Poll the job (via the public `get` API) until it reaches `Done`, or give up
    /// after `within`. Returns how long it took (`Some`), or `None` on timeout.
    async fn wait_until_done(queue: &Queue, id: i64, within: Duration) -> Option<Duration> {
        let start = Instant::now();
        while start.elapsed() < within {
            let state = queue.get(id).await.expect("get job").map(|j| j.state);
            if state == Some(JobState::Done) {
                return Some(start.elapsed());
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        None
    }

    /// Spawn a single worker draining `queue_name` with the given idle poll interval.
    fn spawn_worker(
        queue: Arc<Queue>,
        queue_name: &str,
        poll_interval: Duration,
    ) -> (watch::Sender<bool>, tokio::task::JoinHandle<()>) {
        let (tx, rx) = watch::channel(false);
        let cfg = WorkerConfig {
            queue_name: queue_name.to_string(),
            poll_interval,
            visibility_timeout: Duration::from_secs(30),
            claim_batch: 10,
            retry: RetryPolicy::default(),
        };
        let handle = tokio::spawn(run("w0".into(), queue, cfg, rx));
        (tx, handle)
    }

    /// Sanity: with a fast poll, an *immediately-ready* job drains promptly. This is
    /// the control for [`delayed_job_coming_due_is_not_stranded_by_a_long_poll`] —
    /// it proves the harness (spawn → enqueue → run → done) works, so a failure of
    /// the ignored test below is about the wakeup gap, not the test scaffolding.
    #[sqlx::test]
    async fn ready_job_is_drained_by_a_running_worker(pool: PgPool) {
        let queue = Queue::new(pool.clone(), 5);
        let (shutdown, worker) = spawn_worker(queue.clone(), "now", Duration::from_millis(100));

        let id = queue.enqueue(noop_job("now", 0)).await.expect("enqueue");
        let took = wait_until_done(&queue, id, Duration::from_secs(3)).await;

        let _ = shutdown.send(true);
        let _ = tokio::time::timeout(Duration::from_secs(1), worker).await;
        assert!(took.is_some(), "a ready noop job should drain within 3s");
    }

    /// Regression: an idle worker wakes when a delayed job *comes due*, instead of
    /// stranding behind the poll fallback until its next tick.
    ///
    /// A job delayed 1s behind a 10s poll: `enqueue` NOTIFYs at insert time (too
    /// early — not yet claimable), and nothing NOTIFYs at t+1s when its `run_at`
    /// arrives. Before `wait_for_work` became due-aware this stranded the job for
    /// ~10s; now the idle sleep is bounded by the earliest `ready` job's `run_at`,
    /// so it runs ~1s in. (This started life as a deliberately-`#[ignore]`d RED
    /// driver for that fix — see git history.)
    ///
    /// The sibling claim-vs-`LISTEN` micro-race (a `NOTIFY` lost in the ~1–10ms
    /// window between an empty `claim` and `LISTEN`) is the same bug class; the
    /// due-aware re-query after `LISTEN` also covers it for already-ready jobs.
    #[sqlx::test]
    async fn delayed_job_coming_due_is_not_stranded_by_a_long_poll(pool: PgPool) {
        let queue = Queue::new(pool.clone(), 5);
        // Long poll: NOTIFY is the *only* fast path, so a missing coming-due NOTIFY
        // shows up as a ~10s stall rather than being masked by frequent polling.
        let (shutdown, worker) = spawn_worker(queue.clone(), "due", Duration::from_secs(10));

        // Due in 1s. `enqueue` NOTIFYs now (too early — not yet claimable); nothing
        // NOTIFYs at t+1s when it actually becomes due.
        let id = queue.enqueue(noop_job("due", 1)).await.expect("enqueue");

        // A correct V4 runs it ~1s in. Give a generous 3s; the bug makes it ~10s.
        let took = wait_until_done(&queue, id, Duration::from_secs(3)).await;

        let _ = shutdown.send(true);
        let _ = tokio::time::timeout(Duration::from_secs(1), worker).await;
        assert!(
            took.is_some(),
            "a job delayed 1s never ran within 3s — nothing wakes an idle worker when a \
             delayed job comes due, so it stranded behind the 10s poll fallback"
        );
    }
}
