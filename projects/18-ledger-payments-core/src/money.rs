//! Shared money & domain types — the values the verticals pass around.
//!
//! **Rule zero: money is integer minor units.** An amount is an [`i64`] count of the
//! smallest unit of its currency (cents for USD, pence for GBP). There is no `f64`
//! anywhere on the money path — floating point can't represent `0.10` exactly, and a
//! ledger that's off by a rounding error isn't a ledger. Amounts are *signed*: the
//! sign is the debit/credit direction (V1 picks the convention).

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Identity of an account. A UUID, not a sequence — ids need no coordination.
pub type AccountId = Uuid;

/// Identity of a posted transaction.
pub type TxId = Uuid;

/// A signed amount in the currency's smallest unit (cents, pence, …). Positive and
/// negative are the two sides of a double-entry posting; which sign is a "debit" is
/// the V1 sign convention (record it in `docs/18-design.md`).
pub type Minor = i64;

/// An account: who holds money, in what currency, under what overdraft policy.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Account {
    pub id: AccountId,
    pub name: String,
    /// ISO 4217 code. Every entry on this account shares it (V1).
    pub currency: String,
    /// Whether this account may go below zero. A customer wallet is `false`
    /// (the no-overdraft invariant V2 must hold); a house account may be `true`.
    pub allow_negative: bool,
    pub created_at: DateTime<Utc>,
}

/// Input to `POST /accounts`.
#[derive(Debug, Clone, Deserialize)]
pub struct NewAccount {
    pub name: String,
    pub currency: String,
    #[serde(default)]
    pub allow_negative: bool,
}

/// Input to `POST /transfers`: move `amount` of `currency` from `from` to `to`.
///
/// This is the *intent*; V1 turns it into a balanced two-entry [`TransactionDraft`],
/// and V2 posts it safely under concurrency. `amount` must be strictly positive —
/// the direction is `from → to`, not the sign of the number.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewTransfer {
    pub from: AccountId,
    pub to: AccountId,
    pub amount: Minor,
    pub currency: String,
    #[serde(default)]
    pub reference: Option<String>,
}

/// One immutable line of the ledger, as stored in `entries`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub id: i64,
    pub transaction_id: TxId,
    pub account_id: AccountId,
    /// Signed minor units. The invariant: `Σ amount` over a transaction is `0`.
    pub amount: Minor,
    pub currency: String,
    pub created_at: DateTime<Utc>,
}

/// A posted transaction and its entries — what a successful transfer returns.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PostedTransaction {
    pub id: TxId,
    pub kind: String,
    pub reference: Option<String>,
    pub entries: Vec<Entry>,
    pub created_at: DateTime<Utc>,
}

/// An account's derived balance, in minor units. Always `SUM(entries.amount)` — never
/// a stored, mutated number (see `ledger::Ledger::balance`).
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct Balance {
    pub minor: Minor,
}
