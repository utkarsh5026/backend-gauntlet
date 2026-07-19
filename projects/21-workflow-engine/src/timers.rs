//! V3 — Durable timers.
//!
//! A workflow that says "wait 3 days, then charge the card" cannot hold that delay in
//! a `tokio::sleep` — the process won't live 3 days, and if it dies the sleep is gone.
//! A durable timer is a **persisted due-time**: the `START_TIMER` command writes a row
//! (in the SAME transaction that appends `TIMER_STARTED`, so the timer can never be
//! lost with the process that created it), and a background scanner fires it later by
//! appending `TIMER_FIRED` + scheduling a workflow task. Restart the whole engine and
//! the timer still fires — because it was never in memory to begin with.
//!
//! The invariant this module owns: a timer fires **exactly once** into history. The
//! scanner may run repeatedly and may overlap with a restart, so firing must be
//! idempotent — `(run_id, timer_id)` is the key, and `TIMER_FIRED` lands at most once.

use std::sync::Arc;
use std::time::Duration;

use sqlx::PgPool;
use tokio::sync::watch;

use crate::dispatch::Dispatcher;
use crate::error::AppError;
use crate::model::RunId;

/// A pending timer the scan found due, ready to fire.
#[derive(Debug, Clone)]
pub struct DueTimer {
    pub run_id: RunId,
    pub timer_id: String,
    pub started_event_id: i64,
}

/// The durable timer store, backed by the `timers` table.
pub struct TimerService {
    pool: PgPool,
}

impl TimerService {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Record a durable timer. Call this **inside** the transaction that appends the
    /// `TIMER_STARTED` event (pass that transaction in when you wire it) so the timer
    /// and its event commit atomically — never one without the other.
    ///
    /// TODO(V3): INSERT into `timers` (run_id, timer_id, started_event_id,
    /// fire_at = now() + delay, state='pending'). Do it in the caller's transaction,
    /// not a fresh one, or you break the atomicity that makes the timer durable.
    pub async fn schedule_timer(
        &self,
        run_id: RunId,
        timer_id: &str,
        started_event_id: i64,
        delay: Duration,
    ) -> Result<(), AppError> {
        let _ = (&self.pool, run_id, timer_id, started_event_id, delay);
        todo!("V3: persist a durable timer alongside its TIMER_STARTED event")
    }

    /// Claim up to `limit` timers whose `fire_at` has passed. Use `FOR UPDATE SKIP
    /// LOCKED` so two engine instances never fire the same timer.
    ///
    /// TODO(V3): SELECT pending timers WHERE fire_at <= now() ORDER BY fire_at
    /// FOR UPDATE SKIP LOCKED LIMIT $1. Return them as [`DueTimer`]s.
    pub async fn claim_due(&self, limit: i64) -> Result<Vec<DueTimer>, AppError> {
        let _ = (&self.pool, limit);
        todo!("V3: claim due timers with SKIP LOCKED")
    }

    /// Mark a timer fired so the next scan skips it (idempotent completion).
    ///
    /// TODO(V3): UPDATE timers SET state='fired' WHERE run_id=$1 AND timer_id=$2.
    /// Do this in the same transaction that appends `TIMER_FIRED` and schedules the
    /// wake-up task, so a crash mid-fire either does all three or none.
    pub async fn mark_fired(&self, run_id: RunId, timer_id: &str) -> Result<(), AppError> {
        let _ = (&self.pool, run_id, timer_id);
        todo!("V3: mark a timer fired atomically with its TIMER_FIRED event")
    }
}

/// The background scan loop, spawned from `main` only when `RUN_TIMER_SERVICE=true`
/// (so the bare scaffold serves without this panicking on its first query). It wakes
/// every `interval`, fires whatever is due, and exits when `shutdown` flips.
pub async fn scan_loop(
    timers: Arc<TimerService>,
    dispatcher: Arc<Dispatcher>,
    interval: Duration,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut tick = tokio::time::interval(interval);
    tracing::info!(?interval, "durable timer scan loop started");
    loop {
        tokio::select! {
            _ = tick.tick() => {
                if let Err(e) = fire_due_timers(&timers, &dispatcher).await {
                    tracing::error!(error = %e, "timer scan failed");
                }
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    tracing::info!("timer scan loop draining");
                    break;
                }
            }
        }
    }
}

/// Fire every currently-due timer: for each, in one transaction, append `TIMER_FIRED`,
/// schedule a workflow task so the workflow wakes up, and mark the timer fired.
///
/// TODO(V3): claim due timers ([`TimerService::claim_due`]); for each, append a
/// `TIMER_FIRED` event (via the history store), enqueue a workflow task on the
/// execution's task queue (via the dispatcher), and [`TimerService::mark_fired`] — all
/// atomically, so a mid-fire crash leaves the timer still 'pending' to retry, never a
/// TIMER_FIRED with no wake-up. Bump `metrics::TIMERS_FIRED_TOTAL` per fire.
async fn fire_due_timers(
    timers: &Arc<TimerService>,
    dispatcher: &Arc<Dispatcher>,
) -> Result<(), AppError> {
    let _ = (timers, dispatcher, crate::metrics::TIMERS_FIRED_TOTAL);
    todo!("V3: fire due timers durably — TIMER_FIRED + wake-up task, exactly once")
}
