//! V2 — The balance invariant under concurrency. **Where isolation levels bite.**
//!
//! V1 posts one correct transaction. This module makes the transfer correct when N of
//! them race on the same account. The money-losing bug: two debits both read balance
//! `100`, both approve `60`, both commit — the account is at `-20` and nothing errored.
//! Under `READ COMMITTED` that overdraft is invisible.
//!
//! Two jobs here:
//!   1. Enforce **no-overdraft** *inside* the money-moving transaction (read the
//!      balance and post under an isolation level strong enough that a concurrent
//!      writer can't slip between your check and your write).
//!   2. Turn the resulting serialization conflicts (SQLSTATE `40001`) into **bounded
//!      retries**, not `500`s.
//!
//! Design decision to record in `docs/18-design.md`: `SERIALIZABLE` (optimistic,
//! retry on 40001) vs `SELECT … FOR UPDATE` row locks (pessimistic). Both can be
//! correct; the SPEC grades that you *chose* and know the tradeoff.

use crate::error::AppError;
use crate::ledger::Ledger;
use crate::money::{NewTransfer, PostedTransaction};

/// How V2 executes a transfer. `max_retries` bounds the 40001 retry loop.
#[derive(Debug, Clone)]
pub struct TransferConfig {
    pub max_retries: u32,
    /// Ceiling on a single transfer amount (minor units), from `MAX_TRANSFER_MINOR`.
    pub max_amount: i64,
    /// Where a settled-transfer event is enqueued in the outbox (V4). `Some` wires the
    /// webhook path: the transfer, *inside its own transaction*, calls
    /// [`crate::webhooks::enqueue_settled`] so the event and the money commit together.
    pub webhook_endpoint: Option<String>,
}

/// The outcome of a transfer, including how hard it had to fight for it — the retry
/// count is a graded observability field (log it, count it in `/metrics`).
#[derive(Debug, Clone)]
pub struct TransferOutcome {
    pub transaction: PostedTransaction,
    pub serialization_retries: u32,
}

/// Move money `from → to`, safely under concurrency. The public entry point V3's
/// idempotency layer and the HTTP handler call.
///
/// TODO(V2): the concurrency-safe transfer. Sketch:
///   1. Validate the intent: `amount > 0`, `amount <= cfg.max_amount`, `from != to`,
///      both accounts exist and share `transfer.currency` (reject otherwise — these
///      are 4xx, not retryable).
///   2. In a loop up to `cfg.max_retries`:
///        a. BEGIN a transaction at your chosen isolation level.
///        b. Read the *current* balance of `from` (and any account with a
///           no-overdraft policy that this posting debits).
///        c. If the debit would push a no-overdraft account below zero, ABORT with a
///           clean `AppError::Overdraft` — do NOT retry (it won't get better).
///        d. Post the balanced two-entry draft (reuse the V1 posting logic; you'll
///           likely want a variant that runs on this same `&mut Transaction`).
///        e. COMMIT. On a 40001 serialization failure, roll back and retry the loop,
///           counting the retry. On success, return with the retry count.
///   3. Exhausting the retries is a clean 409/503-style error, never a panic or 500.
///
/// The subtle part: steps (b)–(d) must be atomic *with respect to other transfers*.
/// That is exactly what your isolation choice buys you — make sure the balance you
/// checked in (b) can't be invalidated by a concurrent writer before (e) commits.
pub async fn transfer(
    ledger: &Ledger,
    cfg: &TransferConfig,
    transfer: NewTransfer,
) -> Result<TransferOutcome, AppError> {
    let _ = (ledger, cfg, transfer);
    todo!("V2: serializable transfer with no-overdraft enforcement + bounded 40001 retry")
}

/// Is this a Postgres serialization failure (SQLSTATE 40001) — i.e. *retryable*?
///
/// TODO(V2): match `sqlx::Error::Database(db)` where `db.code()` is `"40001"`
/// (`serialization_failure`) — and consider `"40P01"` (`deadlock_detected`) too.
/// This is the predicate the retry loop keys on; everything else propagates.
pub fn is_serialization_conflict(err: &sqlx::Error) -> bool {
    let _ = err;
    todo!("V2: detect SQLSTATE 40001 (serialization_failure) for the retry loop")
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the invariant under concurrency (wants a real Postgres).
    //   - fire K concurrent transfers that each debit ONE no-overdraft account seeded
    //     with just enough for a few of them; assert the balance NEVER goes negative
    //     and the number that succeeded matches the money that was actually available;
    //   - seed N accounts with a known total, fire a storm of random transfers between
    //     them, then assert Σ balances is unchanged to the cent (money conserved);
    //   - a transfer that must lose the race retries then fails cleanly (an AppError,
    //     not a 500 / not a panic);
    //   - `is_serialization_conflict` returns true for a 40001 and false otherwise.
}
