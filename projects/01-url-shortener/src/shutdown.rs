//! Graceful shutdown: the signal future and the bounded final flush of the
//! click ingestor. Kept out of `main.rs` (wiring only) so the timeout policy —
//! and the tests that pin it — live next to the logic they cover.

use std::time::Duration;

use tracing::{info, warn};

/// How long we wait for the click buffer to flush on shutdown before giving up.
/// Kept comfortably under a typical orchestrator SIGTERM→SIGKILL grace period
/// (k8s default 30s) so we exit cleanly rather than being killed mid-write.
pub(crate) const SHUTDOWN_FLUSH_BUDGET: Duration = Duration::from_secs(5);

/// Result of waiting for the ingestor to flush on shutdown.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ShutdownOutcome {
    /// Buffer flushed and the ingestor task returned within budget.
    Flushed,
    /// The ingestor task panicked while flushing (batch may be lost).
    Panicked,
    /// The flush didn't finish within `budget`; we exit without it.
    TimedOut,
}

/// Wait for the ingestor's final flush, bounded by `budget`.
///
/// The channel closes once every `ClickSink` is dropped, which drives the
/// ingestor's `run` loop to flush what's left and return. We join that task so
/// the process doesn't exit mid-flush. The timeout is the accepted tradeoff: a
/// wedged DB write must not hold us past the SIGKILL deadline, so we'd rather
/// drop a final batch than hang.
pub(crate) async fn drain_ingestor(
    handle: tokio::task::JoinHandle<()>,
    budget: Duration,
) -> ShutdownOutcome {
    match tokio::time::timeout(budget, handle).await {
        Ok(Ok(())) => {
            info!("click buffer flushed, ingestor stopped cleanly");
            ShutdownOutcome::Flushed
        }
        Ok(Err(e)) => {
            warn!(error = %e, "ingestor task panicked during shutdown flush");
            ShutdownOutcome::Panicked
        }
        Err(_) => {
            warn!(
                budget_secs = budget.as_secs(),
                "ingestor flush exceeded shutdown budget; exiting with clicks possibly unflushed"
            );
            ShutdownOutcome::TimedOut
        }
    }
}

pub(crate) async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        use tokio::signal::unix::{signal, SignalKind};
        signal(SignalKind::terminate())
            .expect("failed to install term signal handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    info!("shutdown signal received");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn drain_ingestor_reports_a_clean_flush() {
        // A task that returns promptly stands in for the ingestor finishing its
        // final flush well inside the budget.
        let handle = tokio::spawn(async {});
        let outcome = drain_ingestor(handle, Duration::from_secs(5)).await;
        assert_eq!(outcome, ShutdownOutcome::Flushed);
    }

    #[tokio::test]
    async fn drain_ingestor_gives_up_after_budget() {
        // The ingestor is wedged (e.g. a stuck DB write). We must return near the
        // budget, not block on the task: a 50ms budget against a 30s task proves
        // we don't await it.
        let handle = tokio::spawn(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
        });
        let started = std::time::Instant::now();
        let outcome = drain_ingestor(handle, Duration::from_millis(50)).await;
        assert_eq!(outcome, ShutdownOutcome::TimedOut);
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "returned near the budget, not after the 30s task"
        );
    }

    #[tokio::test]
    async fn drain_ingestor_survives_a_panicking_flush() {
        // A panic in the flush must not take the shutdown path down with it.
        let handle = tokio::spawn(async { panic!("simulated flush failure") });
        let outcome = drain_ingestor(handle, Duration::from_secs(5)).await;
        assert_eq!(outcome, ShutdownOutcome::Panicked);
    }
}
