//! V1 — The double-entry posting engine, from scratch.
//!
//! This is the part you'd normally get from an accounting library. A balance is not
//! a number you increment; it is **derived** from an append-only log of immutable
//! [`Entry`]s. Every money movement is a [`TransactionDraft`] — a set of entries whose
//! signed amounts sum to **exactly zero** (money is conserved: it moves, it is never
//! created or destroyed).
//!
//! The two invariants this module owns:
//!   1. **Balanced:** `Σ entries == 0` for every transaction — rejected before write.
//!   2. **Atomic:** all of a transaction's entries land, or none do.
//!
//! Concurrency safety (no-overdraft, isolation, retry) is *not* here — that's V2 in
//! `isolation.rs`, which calls into this engine. Keep this module about the mechanics
//! of one correct posting.

use std::sync::Arc;

use sqlx::PgPool;

use crate::error::AppError;
use crate::money::{Account, AccountId, Balance, Minor, NewAccount, PostedTransaction, TxId};

/// One line of a draft posting: put `amount` (signed minor units) on `account`.
#[derive(Debug, Clone)]
pub struct EntryDraft {
    pub account_id: AccountId,
    pub amount: Minor,
    pub currency: String,
}

/// A proposed transaction: the entries to post atomically. Build one, hand it to
/// [`Ledger::post`], which validates the balance invariant and writes it in one txn.
#[derive(Debug, Clone)]
pub struct TransactionDraft {
    pub kind: String,
    pub reference: Option<String>,
    pub entries: Vec<EntryDraft>,
}

impl TransactionDraft {
    /// The signed sum of the draft's entries. **Must be zero** to be postable.
    ///
    /// TODO(V1): this is the whole balance invariant in one line. Use it to reject an
    /// unbalanced draft *before* touching the DB (see [`Ledger::post`]).
    pub fn net(&self) -> Minor {
        self.entries.iter().map(|e| e.amount).sum()
    }
}

/// The ledger, backed by the `accounts` / `transactions` / `entries` tables.
pub struct Ledger {
    pool: PgPool,
}

impl Ledger {
    pub fn new(pool: PgPool) -> Arc<Self> {
        Arc::new(Self { pool })
    }

    /// The connection pool, for the verticals that run their own transactions
    /// (the serializable transfer in `isolation.rs`, the outbox in `webhooks.rs`).
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Create an account.
    pub async fn create_account(&self, new: NewAccount) -> Result<Account, AppError> {
        // TODO(V1): INSERT into `accounts` and RETURN the row. Validate `currency`
        // (non-empty, a sane ISO-4217-ish code) before you write it — every entry on
        // this account will have to match it.
        let _ = (&self.pool, new);
        todo!("V1: create an account")
    }

    /// Fetch an account by id (`None` if it doesn't exist).
    pub async fn get_account(&self, id: AccountId) -> Result<Option<Account>, AppError> {
        // TODO(V1): SELECT the account by id.
        let _ = (&self.pool, id);
        todo!("V1: fetch an account by id")
    }

    /// Post a balanced transaction **atomically**. This is the core of V1.
    ///
    /// TODO(V1): the posting engine. In ONE database transaction:
    ///   1. Reject if `draft.net() != 0` — an unbalanced post must write nothing.
    ///   2. Reject if any entry's `currency` differs from its account's currency.
    ///   3. INSERT the `transactions` row, then INSERT every `entries` row.
    ///   4. Commit — so either all entries land or none do (atomicity).
    /// Entries are immutable: there is no update/delete path here or anywhere. A
    /// correction is a *new* reversing transaction (a draft with the signs flipped).
    ///
    /// NOTE: this is the *single-posting* primitive. It does NOT enforce no-overdraft
    /// or serializable isolation — that's V2. V2's transfer runs this logic inside a
    /// SERIALIZABLE transaction with a balance check; you'll likely refactor the SQL
    /// so it can run on a caller-supplied `&mut Transaction`. Design for that.
    pub async fn post(&self, draft: TransactionDraft) -> Result<PostedTransaction, AppError> {
        let _ = (&self.pool, draft);
        todo!("V1: validate the draft balances, then atomically insert txn + entries")
    }

    /// An account's **derived** balance: `SUM(amount)` over its entries.
    ///
    /// TODO(V1): SELECT COALESCE(SUM(amount), 0) FROM entries WHERE account_id = $1.
    /// This is the *only* way a balance is computed — there is no stored balance
    /// column to read. That's what keeps the ledger honest (and auditable).
    pub async fn balance(&self, account_id: AccountId) -> Result<Balance, AppError> {
        let _ = (&self.pool, account_id);
        todo!("V1: derive balance from SUM(entries.amount)")
    }

    /// Fetch a posted transaction and its entries (`None` if it doesn't exist).
    pub async fn get_transaction(&self, id: TxId) -> Result<Option<PostedTransaction>, AppError> {
        // TODO(V1): SELECT the transaction, then its entries; assemble a
        // PostedTransaction. `None` if the transaction id is unknown.
        let _ = (&self.pool, id);
        todo!("V1: fetch a transaction and its entries")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the posting engine (these want a real Postgres — gate them
    // behind a DATABASE_URL fixture). Suggested cases:
    //   - a balanced two-entry transfer posts; balance(from) and balance(to) reflect it;
    //   - an UNBALANCED draft (net != 0) is rejected and writes nothing (row counts
    //     unchanged) — atomicity;
    //   - a cross-currency entry (entry.currency != account.currency) is rejected;
    //   - reported balance == recomputed SUM(entries.amount) for every account.
    //
    // TODO(V1): a proptest — generate random *balanced* batches of transfers, post
    // them all, and assert Σ of every account's balance == 0 (money conserved) and
    // per-transaction net == 0. See `prop_ledger_conserves_money`.
}
