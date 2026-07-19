//! V3 — Load balancing across a backend pool.
//!
//! Module: `src/balancer.rs`. A route points at a *pool*; the balancer picks one
//! backend per request. The scaffold gives you the pool structure, the per-backend
//! bookkeeping (in-flight, EWMA latency, circuit breaker), and the policy enum —
//! `pick()` is the `todo!()`. Start with round-robin, then least-connections, then
//! P2C + EWMA, and prove they diverge under skewed load. See SPEC.md V3.

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;

use serde::Deserialize;

use crate::health::CircuitBreaker;

/// Load-balancing policy over a pool. Deserialized from the route config's `lb`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LbPolicy {
    /// Hand out backends in rotation. Simple, ignores load — the floor.
    #[default]
    RoundRobin,
    /// Route to the backend with the fewest in-flight requests.
    LeastConn,
    /// Power of two choices: sample two at random, pick the less-loaded — the
    /// cheap approximation that avoids round-robin's herd behaviour.
    P2c,
}

/// One upstream backend and its live state.
///
/// The counters and circuit breaker are the *signals* the balancer (V3) and health
/// layer (V4) read. Wiring the accounting into the request path is part of V1/V3.
pub struct Backend {
    /// `host:port` used to build the upstream URI.
    pub addr: String,
    /// Requests currently in flight to this backend (least-conn / P2C signal).
    in_flight: AtomicUsize,
    /// EWMA of recent upstream latency in microseconds (the P2C tie-break signal).
    ewma_micros: AtomicU64,
    /// Per-backend circuit breaker (V4): when open, this backend is skipped.
    pub circuit: CircuitBreaker,
}

impl Backend {
    pub fn new(addr: &str) -> Arc<Self> {
        Arc::new(Self {
            addr: addr.to_string(),
            in_flight: AtomicUsize::new(0),
            ewma_micros: AtomicU64::new(0),
            circuit: CircuitBreaker::new(),
        })
    }

    /// Current in-flight count (least-conn / P2C signal).
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::Relaxed)
    }

    /// Increment when a request is dispatched to this backend (V3).
    pub fn incr_in_flight(&self) {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
    }

    /// Decrement when a request to this backend completes (V3).
    pub fn decr_in_flight(&self) {
        self.in_flight.fetch_sub(1, Ordering::Relaxed);
    }

    /// Last recorded EWMA latency in microseconds (P2C tie-break).
    pub fn ewma_micros(&self) -> u64 {
        self.ewma_micros.load(Ordering::Relaxed)
    }

    /// Is this backend eligible to receive a request (circuit not open)? (V4)
    pub fn is_available(&self) -> bool {
        self.circuit.allow()
    }
}

/// The pool + policy for one route's upstream.
pub struct Balancer {
    policy: LbPolicy,
    backends: Vec<Arc<Backend>>,
    /// Rotation cursor for round-robin.
    rr: AtomicUsize,
}

impl Balancer {
    pub fn new(policy: LbPolicy, backends: Vec<Arc<Backend>>) -> Self {
        Self {
            policy,
            backends,
            rr: AtomicUsize::new(0),
        }
    }

    /// The pool (used by the health checker in V4 to probe every backend).
    pub fn backends(&self) -> &[Arc<Backend>] {
        &self.backends
    }

    pub fn policy(&self) -> LbPolicy {
        self.policy
    }

    /// Pick a backend for the next request, or `None` if the whole pool is
    /// unavailable (→ 503).
    ///
    /// TODO(V3): implement per `self.policy` — round-robin (advance `self.rr`),
    /// least-connections (min `in_flight`), then P2C (two random samples, break the
    /// tie on `in_flight`/`ewma_micros`). It must **skip** any backend whose circuit
    /// is open (`is_available() == false`) and must be cheap on the hot path.
    pub fn pick(&self) -> Option<Arc<Backend>> {
        let _ = (&self.rr, &self.backends, self.policy);
        todo!("V3: choose a healthy backend per the LB policy")
    }
}
