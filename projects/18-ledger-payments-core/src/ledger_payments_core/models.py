"""The money and domain types the verticals pass around, and the API's JSON shapes.

## Rule zero: money is integer minor units

An amount is an `int` count of the smallest unit of its currency — cents for USD,
pence for GBP. There is no `float` anywhere on the money path: a binary float can't
represent `0.10` exactly, and a ledger that is off by a rounding error isn't a ledger.

Python makes the rule easy to keep — its `int` is arbitrary-precision, so an amount
can't overflow either — and easy to break without noticing. A JSON `1000.0` is a
`float`, and pydantic's default *lax* mode would quietly turn it into `1000`. So every
amount here is `Strict`: a float (even an integral one), a numeric string, or a `true`
is a `422` at the boundary, before it can reach a posting. (`bool` is a subclass of
`int` in Python, which is exactly why "is it an int?" is not the question to ask.)

Amounts are *signed*: the sign is the debit/credit direction, and which is which is
the V1 sign convention (record it in `docs/18-design.md`).

## What is *not* validated here

Strictness is the type. Whether an amount is positive, under the configured ceiling,
and in the account's currency are the Security checklist's validation items — and the
ceiling is configuration, so it couldn't be a static `Field` bound anyway. Those are
yours.

## Posted things are frozen

`Entry` and `PostedTransaction` are `frozen` models holding tuples. That doesn't make
the *database* append-only — that's V1 — but it does mean no code in this process can
edit a posted entry in place and still have it look legitimate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, Strict

__all__ = [
    "Account",
    "Balance",
    "Entry",
    "MinorUnits",
    "NewAccount",
    "NewTransfer",
    "PostedTransaction",
]

MinorUnits = Annotated[int, Strict()]
"""A signed amount in its currency's smallest unit. Strict — see the module docs."""


class NewAccount(BaseModel):
    """Input to `POST /accounts`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    currency: str
    """ISO 4217 code. Every entry ever posted to this account has to match it (V1)."""
    allow_negative: bool = False
    """Whether the account may go below zero. A customer wallet is `False` — the
    no-overdraft invariant V2 must hold under any concurrency; a house or settlement
    account may be `True`."""


class Account(BaseModel):
    """An account: who holds money, in what currency, under what overdraft policy."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    currency: str
    allow_negative: bool
    created_at: datetime


class NewTransfer(BaseModel):
    """Input to `POST /transfers`: move `amount` of `currency` from `from` to `to`.

    The *intent*, not a posting: V1 turns it into a balanced two-entry draft, and V2
    posts that safely under concurrency. The direction is `from → to`, never the sign
    of `amount`.

    `extra="forbid"` earns its place beyond tidiness. V3 fingerprints the request, and
    if you fingerprint the *parsed* model rather than the raw bytes, a field this model
    silently dropped would make two different requests look like one.
    """

    # `from` is a keyword in Python, so the attributes are `from_account`/`to_account`
    # and the JSON keeps the natural names through aliases.
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    from_account: UUID = Field(alias="from")
    to_account: UUID = Field(alias="to")
    amount: MinorUnits
    currency: str
    reference: str | None = None
    """An external reference or memo, stored on the transaction."""


class Entry(BaseModel):
    """One immutable line of the ledger, as stored in `entries`."""

    model_config = ConfigDict(frozen=True)

    id: int
    transaction_id: UUID
    account_id: UUID
    amount: MinorUnits
    """Signed. Over the entries of one transaction, these sum to exactly `0`."""
    currency: str
    created_at: datetime


class PostedTransaction(BaseModel):
    """A posted transaction and its entries — what a successful transfer returns."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    reference: str | None
    entries: tuple[Entry, ...]
    created_at: datetime


class Balance(BaseModel):
    """`GET /accounts/{id}/balance`.

    Always derived from the account's entries at read time. There is no stored balance
    to return (V1), and no cached one either — see the Caching checklist for why.
    """

    model_config = ConfigDict(frozen=True)

    account_id: UUID
    currency: str
    minor: int
