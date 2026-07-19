//! V4 — Health checking & circuit breaking.
//!
//! Module: `src/health.rs`. Two mechanisms keep a dying backend from taking the
//! gateway down with it:
//!   * a **circuit breaker** per backend (`Closed → Open → HalfOpen`) so a run of
//!     failures makes calls *fail fast* instead of each waiting the full timeout, and
//!   * an **active health checker** that probes every backend on an interval and
//!     ejects/re-admits it.
//!
//! The scaffold gives you the types, the state enum, and the interfaces the balancer
//! (V3) and proxy (V1) already call (`allow`, `record_success`, `record_failure`).
//! The state machine and the probe loop are the `todo!()`s. See SPEC.md V4.

use std::sync::atomic::AtomicU8;
use std::sync::Arc;
use std::time::Duration;

/// The three circuit-breaker states.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    /// Traffic flows normally; failures are counted.
    Closed,
    /// Tripped: calls fail fast without touching the backend.
    Open,
    /// Cooldown elapsed: a limited number of trial calls are allowed through to
    /// test recovery.
    HalfOpen,
}

/// A per-backend circuit breaker. The atomics are here so `allow()` stays cheap on
/// the hot path (no mutex per request). You'll likely add more counters/timestamps
/// (failure run length, `opened_at`, half-open permits) as you build the machine.
pub struct CircuitBreaker {
    /// Current state, encoded as a `u8` so it's a lock-free load on the hot path.
    state: AtomicU8,
}

impl CircuitBreaker {
    pub fn new() -> Self {
        Self {
            // 0 = Closed to start.
            state: AtomicU8::new(0),
        }
    }

    /// May a request go to this backend right now? `false` when the circuit is
    /// **open** (fail fast). Called by the balancer (V3) before selecting a backend
    /// and on the proxy hot path (V1).
    ///
    /// TODO(V4): return `false` while open; when the open cooldown has elapsed, move
    /// to half-open and allow a *limited* number of trial requests.
    pub fn allow(&self) -> bool {
        let _ = &self.state;
        todo!("V4: gate requests on the circuit state (fail fast when open)")
    }

    /// Record a successful upstream call. In half-open, enough successes should
    /// **close** the circuit. TODO(V4).
    pub fn record_success(&self) {
        todo!("V4: count success; close the circuit from half-open")
    }

    /// Record a failed/timed-out upstream call. Past the failure threshold this
    /// should **open** the circuit; a failure in half-open should re-open it. TODO(V4).
    pub fn record_failure(&self) {
        todo!("V4: count failure; open the circuit past the threshold")
    }

    /// Current state (for `/metrics` and structured logs). TODO(V4).
    pub fn state(&self) -> CircuitState {
        todo!("V4: report the current circuit state")
    }
}

impl Default for CircuitBreaker {
    fn default() -> Self {
        Self::new()
    }
}

/// The background active health checker: probes every backend on an interval and
/// flips its circuit/eligibility so the balancer stops selecting a dead one.
///
/// Constructed in `main` (wired) but **not spawned** in the scaffold — spawn it once
/// `run()` exists (see the `TODO(V4)` in `main.rs`).
pub struct HealthChecker {
    router: Arc<crate::router::Router>,
    client: crate::UpstreamClient,
    interval: Duration,
}

impl HealthChecker {
    pub fn new(
        router: Arc<crate::router::Router>,
        client: crate::UpstreamClient,
        interval: Duration,
    ) -> Self {
        Self {
            router,
            client,
            interval,
        }
    }

    /// The probe loop: every `interval`, send a health probe to each backend
    /// (`router.backends()`), and eject/re-admit it via its circuit breaker.
    ///
    /// TODO(V4): implement the ticker + probe, updating each backend's state so a
    /// dead one is skipped by `Balancer::pick` and a recovered one is re-added.
    pub async fn run(self) {
        let _ = (&self.router, &self.client, self.interval);
        todo!("V4: probe every backend on an interval; eject dead, re-admit recovered")
    }
}
