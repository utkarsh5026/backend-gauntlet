"""V2 — The balance invariant under concurrency. **Where isolation levels bite.**

V1 posts one correct transaction. This module makes a transfer correct when N of them
race on the same account. The money-losing bug: two debits both read balance `100`,
both approve `60`, both commit — the account is at `-20`, and nothing errored. Under
`READ COMMITTED`, which is Postgres' default and therefore asyncpg's, that overdraft
is invisible.

Two jobs here:

1. Enforce **no-overdraft** *inside* the money-moving transaction: read the balance
   and post under an isolation level (or a lock) strong enough that a concurrent
   writer can't slip between your check and your write.
2. Turn the resulting conflicts — SQLSTATE `40001` `serialization_failure`, and
   `40P01` `deadlock_detected` — into **bounded retries**, not `500`s.

Design decision to record in `docs/18-design.md`: `SERIALIZABLE` (optimistic: retry on
conflict) vs `SELECT … FOR UPDATE` row locks (pessimistic: wait). Both can be correct;
the SPEC grades that you *chose*, and can name the tradeoff.

## The trap in the retry loop

A serialization failure doesn't have to come from the statement that caused it.
Postgres may raise it on a later statement or at `COMMIT` — and in asyncpg the commit
happens in the *exit* of `async with conn.transaction(…)`. So the unit you retry is
the **whole transaction block**, re-reading everything from scratch on each attempt.
A `try` around one `execute` catches nothing useful, and a retry that reuses a balance
read by an earlier attempt has reintroduced the bug it was retrying around.

## Proof (see SPEC V2)

* K concurrent transfers debiting ONE no-overdraft account seeded for only a few of
  them: the balance never goes negative, and the number that succeeded matches the
  money that was there. Give every racer its **own** connection — a shared one would
  serialize them for you and hide the race.
* N accounts with a known total and a storm of random transfers between them: `Σ`
  balances unchanged to the cent.
* A transfer that must lose the race retries, then fails cleanly with an `AppError`.
* `is_serialization_conflict` is true for `40001` (and your call on `40P01`), false for
  everything else.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .models import NewTransfer, PostedTransaction

__all__ = ["TransferOutcome", "TransferPolicy", "is_serialization_conflict", "transfer"]


@dataclass(frozen=True, slots=True)
class TransferPolicy:
    """How a transfer is executed — built once from `Settings` in `main`."""

    max_retries: int
    """Bounds the conflict retry loop (`MAX_SERIALIZATION_RETRIES`)."""
    max_amount: int
    """Ceiling on one transfer, in minor units (`MAX_TRANSFER_MINOR`)."""
    webhook_endpoint: str | None = None
    """Where a settled transfer's event goes (V4). When set, the transfer calls
    `webhooks.enqueue_settled` *on its own connection, inside its own transaction*, so
    the event and the money commit together or not at all."""


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """A settled transfer, and how hard it had to fight for it."""

    transaction: PostedTransaction
    serialization_retries: int
    """A graded observability field: log it on the transfer's line and count it at
    `/metrics`. A retry count that climbs is contention you can see before it's p99."""


async def transfer(ledger: Ledger, policy: TransferPolicy, new: NewTransfer) -> TransferOutcome:
    """Move money `from → to`, safely under concurrency. What the HTTP handler calls.

    TODO(V2): the concurrency-safe transfer.
      1. Validate the intent: `amount > 0`, `amount <= policy.max_amount`, `from != to`,
         both accounts exist and share `new.currency`. These raise `BadRequestError` /
         `NotFoundError` — 4xx, never retried.
      2. For up to `policy.max_retries` retries, each attempt a fresh transaction:
           a. acquire a connection and open `conn.transaction(isolation=…)` at your
              chosen level (asyncpg spells them `"serializable"`,
              `"repeatable_read"`, `"read_committed"`);
           b. read the *current* balance of every no-overdraft account this posting
              debits — on this connection, inside this transaction;
           c. if the debit would take one below zero, raise `InsufficientFundsError`.
              Don't retry it: it won't get better;
           d. post V1's `draft_transfer(new)` with `post_on(conn, …)`, then (V4)
              enqueue the settlement event on the same `conn`;
           e. let the block commit. If it raised and `is_serialization_conflict(exc)`,
              count a retry, back off briefly *with jitter* — N losers that retry in
              lockstep just collide again — and go round. `asyncio.sleep`, never
              `time.sleep`: that one stops every transfer in the process.
      3. Out of retries: raise `RetriesExhaustedError`. Never a bare 500.

    Steps b–d must be atomic *with respect to other transfers*. That is exactly what
    your isolation choice buys you: make sure the balance you checked in (b) can't be
    invalidated by a concurrent writer before (e) commits.
    """
    raise NotImplementedError(
        "V2: serializable transfer with no-overdraft enforcement + bounded conflict retry"
    )


def is_serialization_conflict(exc: BaseException) -> bool:
    """Is `exc` a *retryable* Postgres conflict?

    TODO(V2): the predicate the retry loop keys on — everything else propagates.
    asyncpg maps SQLSTATEs to exception classes (`40001` is
    `asyncpg.exceptions.SerializationError`, `40P01` is `DeadlockDetectedError`), and
    every `asyncpg.PostgresError` also carries the raw code as `.sqlstate`. Decide
    which you match on, and whether a deadlock counts — under `FOR UPDATE` it's the
    only conflict you'll see.
    """
    raise NotImplementedError("V2: detect a serialization failure (SQLSTATE 40001) to retry")
