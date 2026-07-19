//! V4 — Signed webhooks with retries, via the **transactional outbox**.
//!
//! When a transfer settles, the merchant's endpoint must hear about it — and that
//! endpoint may be down, slow, or hostile. Three things must all hold:
//!   1. **Atomic with the money:** the event is enqueued in the *same DB transaction*
//!      as the posting, so a posting and its event commit together or not at all
//!      (`webhook_outbox`). This is why we don't just fire the HTTP call after commit —
//!      a crash in that gap loses the event with no trace.
//!   2. **Signed:** each delivery carries `X-Signature = HMAC-SHA256(secret, ts.body)`
//!      and `X-Timestamp`, so the receiver can verify it's really you and reject
//!      replays. The secret is never logged; verification is a constant-time compare.
//!   3. **At-least-once with backoff:** a failed delivery retries with exponential
//!      backoff + jitter up to a cap, then dead-letters. Down ≠ lost; and because it's
//!      at-least-once, the *receiver* must be idempotent too (V3 comes back around).
//!
//! The dispatcher is a background loop (gated by `RUN_DISPATCHER`) that claims pending
//! rows with `FOR UPDATE SKIP LOCKED`, delivers them, and reschedules or dead-letters.
//! Because state lives in Postgres, delivery survives a restart.

use std::time::Duration;

use sqlx::PgPool;
use tokio::sync::watch;

use crate::error::AppError;
use crate::money::TxId;

/// Dispatcher tuning, from env. `signing_secret` is a secret — never log it.
#[derive(Clone)]
pub struct WebhookConfig {
    pub signing_secret: String,
    pub endpoint_url: String,
    pub max_attempts: i32,
    pub dispatch_interval: Duration,
    pub dispatch_batch: i64,
}

// Keep the secret out of any accidental `{:?}` in a log line.
impl std::fmt::Debug for WebhookConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WebhookConfig")
            .field("signing_secret", &"<redacted>")
            .field("endpoint_url", &self.endpoint_url)
            .field("max_attempts", &self.max_attempts)
            .field("dispatch_interval", &self.dispatch_interval)
            .field("dispatch_batch", &self.dispatch_batch)
            .finish()
    }
}

/// Enqueue a settlement event **on an existing transaction** — this is what makes the
/// outbox atomic with the posting. V2's transfer calls this with the *same* `&mut tx`
/// it posted the entries on, so the event and the money commit together.
///
/// TODO(V4): INSERT a `webhook_outbox` row (event_type, payload JSON, endpoint_url,
/// state='pending', next_attempt_at=now()) using the passed-in transaction handle —
/// NOT a fresh pool connection, or you've broken atomicity. Do not deliver here; the
/// dispatcher does that out of band.
pub async fn enqueue_settled<'c>(
    tx: &mut sqlx::Transaction<'c, sqlx::Postgres>,
    endpoint_url: &str,
    transaction_id: TxId,
    payload: &serde_json::Value,
) -> Result<(), AppError> {
    let _ = (tx, endpoint_url, transaction_id, payload);
    todo!("V4: INSERT the settlement event into webhook_outbox on the posting's txn")
}

/// Compute the delivery signature: `HMAC-SHA256(secret, "{timestamp}.{body}")`, hex.
///
/// TODO(V4): use `hmac::Hmac<sha2::Sha256>` keyed with `secret`, feed it
/// `format!("{timestamp}.{body}")`, and hex-encode the tag. This is the value that
/// goes in the `X-Signature` header; the receiver recomputes it and compares.
pub fn sign(secret: &[u8], timestamp: i64, body: &[u8]) -> String {
    let _ = (secret, timestamp, body);
    todo!("V4: HMAC-SHA256 over (timestamp, body), hex-encoded")
}

/// Verify a delivery signature in **constant time** (defends against timing oracles).
///
/// TODO(V4): recompute [`sign`] and compare with a constant-time equality (HMAC's own
/// `verify_slice`, or a constant-time byte compare) — never `==` on the hex strings.
/// Also reject a `timestamp` outside an allowed skew window to defeat replay.
pub fn verify(secret: &[u8], timestamp: i64, body: &[u8], signature: &str) -> bool {
    let _ = (secret, timestamp, body, signature);
    todo!("V4: constant-time verify of the HMAC signature (+ timestamp skew check)")
}

/// The backoff before attempt `attempt` (1-based): exponential, capped, jittered.
///
/// TODO(V4): return something like `min(base * 2^(attempt-1), cap)` plus random
/// jitter, so a fleet of failed deliveries doesn't resynchronize into a retry wave.
/// Keep it monotonic-in-expectation and capped — proptest it.
pub fn backoff(attempt: i32) -> Duration {
    let _ = attempt;
    todo!("V4: exponential backoff with a cap and jitter")
}

/// The dispatcher loop: claim pending events, deliver, reschedule or dead-letter.
/// Spawned as a background task in `main` when `RUN_DISPATCHER=true`; runs until the
/// shutdown watch flips.
///
/// TODO(V4): each tick (every `cfg.dispatch_interval`, until `shutdown` is true):
///   1. Claim a batch of due events:
///        SELECT ... FROM webhook_outbox
///        WHERE state='pending' AND next_attempt_at <= now()
///        ORDER BY next_attempt_at
///        FOR UPDATE SKIP LOCKED
///        LIMIT cfg.dispatch_batch
///      (SKIP LOCKED so multiple dispatchers never grab the same event.)
///   2. For each: sign + POST it to `endpoint_url` with `X-Signature`/`X-Timestamp`.
///        - 2xx  -> mark state='delivered'.
///        - else -> attempts += 1; if attempts >= max_attempts, state='dead' (DLQ);
///                  otherwise next_attempt_at = now() + backoff(attempts), state stays
///                  'pending'. Record `last_error`.
///   3. On shutdown, finish the current batch, then return (don't abandon in-flight).
/// Count delivered / failed / dead and the outbox lag in `/metrics`.
pub async fn dispatch_loop(pool: PgPool, cfg: WebhookConfig, mut shutdown: watch::Receiver<bool>) {
    // Wired to compile & shut down cleanly; the delivery logic is the V4 worklist.
    let _ = (&pool, &cfg);
    loop {
        if *shutdown.borrow() {
            break;
        }
        // TODO(V4): claim a batch and deliver it (see the sketch above), then wait
        // for the next tick OR a shutdown signal — whichever comes first.
        tokio::select! {
            _ = tokio::time::sleep(cfg.dispatch_interval) => {
                todo!("V4: claim due outbox events (FOR UPDATE SKIP LOCKED), sign, deliver, reschedule/DLQ");
            }
            _ = shutdown.changed() => {}
        }
    }
    tracing::info!("webhook dispatcher stopped");
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the delivery guarantees.
    //   - sign/verify round-trips; a tampered body fails verify; a stale timestamp is
    //     rejected;
    //   - backoff is monotonic-in-expectation, capped, and jittered (proptest);
    //   - a down receiver (point at a server that 500s / never answers) causes
    //     attempts to climb with growing next_attempt_at, then a 'dead' DLQ row after
    //     max_attempts — never a tight infinite loop;
    //   - the outbox insert and the posting commit atomically: roll back the txn and
    //     BOTH the entries and the event are gone (wants a real Postgres).
}
