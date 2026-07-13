//! Prometheus metrics for the observability checklist (see SPEC.md → Horizontal
//! checklist → Observability, and `docs/06-backend-fundamentals.md` §2).
//!
//! Same pattern as `01-url-shortener/src/metrics.rs`: the [`metrics`] facade
//! writes to a process-global recorder, so call sites elsewhere in the crate
//! (`queue.rs`, `worker.rs`, `lease.rs`, `retry.rs`) just name a metric and stay
//! decoupled from this wiring. Until [`install`] runs, the macros are no-ops —
//! exactly what tests want.
//!
//! [`install`] returns a [`PrometheusHandle`]; render it from `/metrics` (see
//! `routes::metrics_router`) for a scrape endpoint.
//!
//! ## What's wired vs. what's a TODO
//! The recorder + `/metrics` endpoint are wired below. The actual
//! `metrics::counter!`/`gauge!`/`histogram!` *call sites* are **not** — that's
//! the exercise. Look for `TODO(observability)` comments in:
//! - `queue.rs` — `enqueue` (count) and `claim` (empty-claim count)
//! - `worker.rs` — `process_one` (execution-time histogram, completed counter)
//! - `retry.rs` — `nack` (retried / dead-lettered counters)
//! - `lease.rs` — `reap_expired` (leases-reaped counter)
//!
//! The **gauges** (queue depth, in-flight, DLQ size, oldest-ready-age) are a
//! different shape: they're not tied to a single event, they're the *current
//! state* of the `jobs` table. [`sample_gauges`] is where that state gets read
//! and published — see its doc comment for the design question to answer
//! before filling it in.

use std::time::Duration;

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};
use sqlx::PgPool;
use tokio::sync::watch;

use crate::error::AppError;

// ---- Counters (rates) ------------------------------------------------------

/// Jobs successfully enqueued. Increment in `Queue::enqueue` after the insert
/// commits. Consider a `queue` label so multi-queue deployments can split it.
pub const ENQUEUED_TOTAL: &str = "job_queue_enqueued_total";

/// Jobs that reached `done` via a worker's ack. Increment in
/// `worker::process_one`'s success branch, alongside the existing
/// `span.record("outcome", "done")`.
pub const COMPLETED_TOTAL: &str = "job_queue_completed_total";

/// Failures that were rescheduled with backoff (not yet out of attempts).
/// Increment in `retry::nack` when the [`crate::retry::Disposition`] is
/// `Retried`.
pub const RETRIED_TOTAL: &str = "job_queue_retried_total";

/// Failures that exhausted `max_attempts` and landed in the DLQ. Increment in
/// `retry::nack` when the disposition is `DeadLettered` — a non-zero rate here
/// is the "something is permanently broken" signal.
pub const DEAD_LETTERED_TOTAL: &str = "job_queue_dead_lettered_total";

/// Expired leases the reaper returned to `ready`. Increment in
/// `lease::reap_expired` by the row count it already computes. Per the SPEC: a
/// non-zero reap rate means workers are dying *or* the lease is too short.
pub const LEASES_REAPED_TOTAL: &str = "job_queue_leases_reaped_total";

/// Claims that came back empty (no due, ready work). Increment in
/// `Queue::claim` when the returned batch is empty — high-frequency empty
/// claims on an idle queue is exactly the busy-poll cost V4's LISTEN/NOTIFY
/// is supposed to avoid, so this is the metric that proves that win.
pub const CLAIMS_EMPTY_TOTAL: &str = "job_queue_claims_empty_total";

// ---- Gauges (current state) ------------------------------------------------

/// Current count of `ready` jobs (due or not) on a queue.
pub const READY_DEPTH: &str = "job_queue_ready_depth";

/// Current count of `running` (claimed, leased) jobs — in-flight work.
pub const RUNNING_DEPTH: &str = "job_queue_running_depth";

/// Current count of `dead` (DLQ) jobs.
pub const DLQ_DEPTH: &str = "job_queue_dlq_depth";

/// Age, in seconds, of the oldest **due** `ready` job (`run_at <= now()`).
/// *The* lag metric — see `docs/06-backend-fundamentals.md` §2 for why this
/// beats plain depth: a steady depth can hide a queue that's steadily falling
/// behind. Report `0` (not absent) when there's no ready backlog, so a scrape
/// can't confuse "no data yet" with "caught up".
pub const OLDEST_READY_AGE_SECONDS: &str = "job_queue_oldest_ready_age_seconds";

// ---- Histograms (distributions) -------------------------------------------

/// Job handler execution time, in seconds. Record in `worker::process_one`
/// from the `elapsed_ms` it already measures via `Instant::now()`.
pub const EXECUTION_SECONDS: &str = "job_queue_execution_seconds";

/// End-to-end latency: `enqueue`'s `run_at`/insert time to the worker's ack.
/// Needs a timestamp to diff against — `jobs.run_at` isn't quite it (a delayed
/// job's clock should start at `run_at`, not at `enqueue`); think about whether
/// that means a new column, or deriving it some other way.
pub const END_TO_END_LATENCY_SECONDS: &str = "job_queue_end_to_end_latency_seconds";

/// Install the process-global Prometheus recorder and return a handle used to
/// render the registry for `/metrics`. Call once, from `main`, after telemetry
/// init. Panics if a recorder is already installed (calling it twice is a bug).
pub fn install() -> PrometheusHandle {
    register_descriptions();
    PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder")
}

/// Register HELP metadata for every metric above, so `/metrics` output is
/// self-documenting to a scraper. Call sites only pass the name constant;
/// describing them here keeps the strings single-sourced.
fn register_descriptions() {
    metrics::describe_counter!(ENQUEUED_TOTAL, "Jobs successfully enqueued");
    metrics::describe_counter!(COMPLETED_TOTAL, "Jobs acked done by a worker");
    metrics::describe_counter!(RETRIED_TOTAL, "Failures rescheduled with backoff");
    metrics::describe_counter!(
        DEAD_LETTERED_TOTAL,
        "Failures that exhausted max_attempts and landed in the DLQ"
    );
    metrics::describe_counter!(
        LEASES_REAPED_TOTAL,
        "Expired leases returned to ready by the reaper"
    );
    metrics::describe_counter!(CLAIMS_EMPTY_TOTAL, "Claims that found no due, ready work");
    metrics::describe_gauge!(READY_DEPTH, "Current count of ready jobs");
    metrics::describe_gauge!(RUNNING_DEPTH, "Current count of running (leased) jobs");
    metrics::describe_gauge!(DLQ_DEPTH, "Current count of dead-lettered jobs");
    metrics::describe_gauge!(
        OLDEST_READY_AGE_SECONDS,
        "Age in seconds of the oldest due ready job — the queue lag signal"
    );
    metrics::describe_histogram!(EXECUTION_SECONDS, "Job handler execution time, seconds");
    metrics::describe_histogram!(
        END_TO_END_LATENCY_SECONDS,
        "Enqueue-to-done latency, seconds"
    );
}

/// Read the current state of the `jobs` table for `queue` and publish the four
/// gauges above.
///
/// **Design question to answer before implementing this:** gauges reflect
/// *current* state, not events, so nothing in `queue.rs`/`worker.rs` naturally
/// triggers them. Two honest options:
/// 1. Poll `jobs` periodically from a background task (see [`gauge_loop`]) —
///    simple, but the numbers are stale between ticks.
/// 2. Compute them lazily inside the `/metrics` handler itself, on every
///    scrape — always fresh, but a slow scraper now runs a query against your
///    primary table.
///
/// Either is defensible; the SPEC doesn't grade which, only that the numbers
/// exist and are honest. Whichever you pick, one query per gauge (or one query
/// with `GROUP BY state` for the three depth gauges, plus a second for the
/// lag) beats four separate round-trips.
///
/// # Errors
/// Returns [`AppError::Db`] if the underlying query fails.
pub async fn sample_gauges(pool: &PgPool, queue: &str) -> Result<(), AppError> {
    let row = sqlx::query!(
        r#"
        SELECT COUNT(*) FROM jobs WHERE state = 'ready'
        "#,
    )
    .fetch_one(pool)
    .await?;
    let ready_depth = row.count.unwrap_or(0);
    metrics::gauge!(crate::metrics::READY_DEPTH, "queue" => queue.to_string())
        .set(ready_depth as f64);
    Ok(())
}

/// Periodic gauge sampler, mirroring `lease::reap_loop`'s shape. Not wired
/// into `main.rs` yet — spawn it the same way once [`sample_gauges`] is real,
/// so the bare scaffold keeps serving cleanly until then.
pub async fn gauge_loop(
    pool: PgPool,
    queue: String,
    interval: Duration,
    mut shutdown: watch::Receiver<bool>,
) {
    tracing::info!(?interval, "gauge sampler started");
    let mut ticker = tokio::time::interval(interval);
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                if let Err(e) = sample_gauges(&pool, &queue).await {
                    tracing::error!(error = %e, "gauge sample failed");
                }
            }
            _ = shutdown.changed() => {
                tracing::debug!("gauge sampler shutting down");
                break;
            }
        }
    }
}
