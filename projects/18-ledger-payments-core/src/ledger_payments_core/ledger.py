"""V1 — The double-entry posting engine, from scratch.

This is the part you'd normally get from an accounting library. A balance is not a
number you increment; it is **derived** from an append-only log of immutable entries.
Every money movement is a `TransactionDraft` — a set of entries whose signed amounts
sum to **exactly zero** (money is conserved: it moves, it is never created or
destroyed).

The two invariants this module owns:

1. **Balanced:** `Σ entries == 0` for every transaction — rejected before any write.
2. **Atomic:** all of a transaction's entries land, or none do.

Concurrency safety — no-overdraft, isolation, retries — is *not* here. That's V2 in
`isolation.py`, which calls into this engine. Keep this module about the mechanics of
one correct posting.

## Two shapes of the same operation

`Ledger` holds the pool and serves the HTTP reads. `post_on` takes a single `Conn`:
it is the posting itself, for a caller that has *already* opened the transaction it
must run inside. V2 needs exactly that — its balance check, the posting, and V4's
outbox insert have to commit as one — so build `Ledger.post` as "open a transaction,
call `post_on`", not the other way round. (`db.Conn` explains why a function that
takes the pool can't join its caller's transaction.)

## Python notes

* An asyncpg connection runs **one statement at a time**. Inserting the entries with
  `asyncio.gather` over one connection isn't parallel, it's an `InterfaceError`
  ("another operation is in progress"). `conn.executemany` is the batch.
* `async with conn.transaction():` commits on a clean exit and rolls back on *any*
  exception. That's the atomicity guarantee in one statement — what's left to check
  is that nothing you did *outside* the block needed rolling back too.

## Proof (see SPEC V1)

* A balanced two-entry transfer posts, and both balances reflect it.
* An UNBALANCED draft is rejected and writes nothing — row counts unchanged.
* A cross-currency entry is rejected.
* Reported balance == `SUM(entries.amount)` recomputed, for every account.
* A hypothesis property test: random *balanced* batches keep `Σ entries == 0` per
  transaction and across the whole ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from .db import Conn
from .models import Account, NewAccount, NewTransfer, PostedTransaction

__all__ = ["EntryDraft", "Ledger", "TransactionDraft", "draft_transfer", "post_on"]


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """One line of a proposed posting: put `amount` (signed minor units) on `account_id`."""

    account_id: UUID
    amount: int
    currency: str


@dataclass(frozen=True, slots=True)
class TransactionDraft:
    """A proposed transaction: the entries to post atomically.

    Frozen, holding a tuple, so a draft can't be edited between the balance check and
    the write.
    """

    entries: tuple[EntryDraft, ...]
    kind: str = "transfer"
    """`transfer`, `reversal`, … — a correction is a *new* transaction of its own kind,
    never an edit to a posted one."""
    reference: str | None = None

    @property
    def net(self) -> int:
        """The signed sum of the entries. **Must be exactly zero** to be postable.

        The whole balance invariant in one line — reject on it *before* touching the
        database. Exact, because `int` is: this is where rule zero pays for itself.
        """
        return sum(entry.amount for entry in self.entries)


def draft_transfer(transfer: NewTransfer) -> TransactionDraft:
    """Turn a transfer *intent* into the balanced two-entry draft that posts it.

    TODO(V1): this function is your sign convention, written down. One entry takes
    `amount` off `transfer.from_account`, the other puts it on `transfer.to_account`;
    which of the two is negative is the decision `docs/18-design.md` records. It's pure
    — no I/O — so it's the first thing to property-test: for any positive amount, the
    draft's `net` is `0` and each account moves by exactly `amount`.
    """
    raise NotImplementedError("V1: turn a transfer into a balanced two-entry draft")


async def post_on(conn: Conn, draft: TransactionDraft) -> PostedTransaction:
    """Post `draft` on `conn`, which the caller has **already** put in a transaction.

    TODO(V1): the posting engine.
      1. Reject if `draft.net != 0` — an unbalanced draft must write *nothing*, so this
         check comes before the first statement.
      2. Reject an entry whose account doesn't exist, or whose currency differs from
         its account's.
      3. INSERT the `transactions` row (`RETURNING` its id and `created_at`), then
         every `entries` row.
      4. Return the `PostedTransaction` as stored, entries included.
    Don't open a transaction or commit here: the caller owns that boundary, which is
    what lets V2 and V4 run their statements inside the same one. Entries are
    immutable — there is no update or delete path, here or anywhere.
    """
    raise NotImplementedError("V1: validate the draft balances, then insert it and its entries")


class Ledger:
    """The ledger over the `accounts` / `transactions` / `entries` tables."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool[asyncpg.Record]:
        """For the verticals that open their own transactions (V2's transfer)."""
        return self._pool

    async def create_account(self, new: NewAccount) -> Account:
        """Create an account.

        TODO(V1): INSERT into `accounts … RETURNING` the row and build an `Account` from
        it. Validate `currency` first — non-empty and ISO-4217-shaped — because every
        entry ever posted to this account will have to match it.
        """
        raise NotImplementedError("V1: create an account")

    async def get_account(self, account_id: UUID) -> Account | None:
        """TODO(V1): SELECT the account by id; `None` when there isn't one."""
        raise NotImplementedError("V1: fetch an account by id")

    async def post(self, draft: TransactionDraft) -> PostedTransaction:
        """Post a balanced transaction **atomically** — the single-posting primitive.

        TODO(V1): acquire a connection, open a transaction, and hand the connection to
        `post_on`. Either every entry lands or none do, even if the process dies
        mid-write.

        This does NOT enforce no-overdraft or choose an isolation level; that's V2,
        which composes `post_on` inside its own transaction instead of calling this.
        """
        raise NotImplementedError("V1: post a draft inside one transaction")

    async def balance(self, account_id: UUID) -> int:
        """An account's **derived** balance, in minor units.

        TODO(V1): `SELECT COALESCE(SUM(amount), 0) FROM entries WHERE account_id = $1`.
        This is the *only* way a balance is ever computed — there is no stored balance
        column to read, and that is what keeps the ledger honest and auditable.

        Two things to notice when you run it. In Postgres `SUM` over a `BIGINT` is a
        `numeric`, so asyncpg hands it back as a `Decimal`, not an `int` — convert at
        the boundary, or cast in the SQL. And it scans every entry the account has
        ever had, until you add the index `migrations/0001_init.sql` leaves out on
        purpose (measure before and after).

        V2 needs this same read *inside* its transaction, on its connection — decide
        how the two share the SQL.
        """
        raise NotImplementedError("V1: derive the balance from SUM(entries.amount)")

    async def get_transaction(self, transaction_id: UUID) -> PostedTransaction | None:
        """TODO(V1): SELECT the transaction, then its entries in a stable order;
        `None` when the id is unknown."""
        raise NotImplementedError("V1: fetch a transaction and its entries")
