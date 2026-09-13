"""HTTP surface: create accounts, move money, read balances and transactions.

Handlers are thin and wired; what they call — `Ledger`, `isolation.transfer`,
`IdempotencyStore.run` — is where the verticals live. Run it as-is and `POST
/accounts` answers `501` naming the V1 todo, and `POST /transfers` the V2 one (or V3's,
once it carries an `Idempotency-Key`). That's the worklist.

What the framework does before a handler runs, all on the checklist: a body missing a
field, an id that isn't a UUID, or an amount that isn't a strict integer is a `422`.
The return annotations publish every response shape at `/docs`.

TODO(security): nothing here is authenticated yet — see `auth.py`.
TODO(observability): each transfer logs from/to account, amount, transaction id and
serialization-retry count as structured fields, and feeds `metrics`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .errors import NotFoundError
from .idempotency import StoredResponse, fingerprint
from .isolation import transfer
from .models import Account, Balance, NewAccount, NewTransfer, PostedTransaction
from .state import StateDep

__all__ = ["router"]

router = APIRouter()

IdempotencyKey = Annotated[str | None, Header(min_length=1, max_length=255)]
"""The `Idempotency-Key` header — FastAPI maps a parameter named `idempotency_key` to it,
case-insensitively. Bounded, because the key becomes a primary key and a Redis key, and
an unbounded client string in either is a storage-exhaustion vector."""


@router.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    """Liveness. Touches nothing — not Postgres, not Redis — and needs no auth: the
    orchestrator's probe has to reach it."""
    return "ok"


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(body: NewAccount, state: StateDep) -> Account:
    """`POST /accounts` — create an account (V1)."""
    return await state.ledger.create_account(body)


@router.get("/accounts/{account_id}/balance")
async def get_balance(account_id: UUID, state: StateDep) -> Balance:
    """`GET /accounts/{id}/balance` — derived from the entries at read time (V1).

    The account is looked up first so an unknown id is a clean `404` rather than a
    balance of `0`: a sum over no entries is zero whether or not the account exists.
    """
    account = await state.ledger.get_account(account_id)
    if account is None:
        raise NotFoundError("account not found")
    minor = await state.ledger.balance(account_id)
    return Balance(account_id=account.id, currency=account.currency, minor=minor)


@router.get("/transactions/{transaction_id}")
async def get_transaction(transaction_id: UUID, state: StateDep) -> PostedTransaction:
    """`GET /transactions/{id}` — a posted transaction and its entries (V1)."""
    posted = await state.ledger.get_transaction(transaction_id)
    if posted is None:
        raise NotFoundError("transaction not found")
    return posted


@router.post(
    "/transfers",
    status_code=status.HTTP_201_CREATED,
    response_model=PostedTransaction,
    responses={
        200: {"model": PostedTransaction, "description": "A replay of an earlier request"},
        402: {"description": "A no-overdraft account can't cover the debit"},
        409: {"description": "An idempotency key conflict, or contention retries exhausted"},
    },
)
async def create_transfer(
    body: NewTransfer,
    request: Request,
    state: StateDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """`POST /transfers` — move money A → B (V2), deduplicated on `Idempotency-Key` (V3).

    Status codes are deliberate: `201` for a fresh posting, `200` for a replay, `409`
    for a key conflict. A client that sends no key opts out of deduplication and owns
    its own double-charge risk — that's the point of making the key explicit.

    The fingerprint is over the raw request bytes. FastAPI already read them to parse
    `body`, and Starlette caches them on the request, so reading them again here is not
    a second receive.
    """

    async def execute() -> StoredResponse:
        outcome = await transfer(state.ledger, state.policy, body)
        return StoredResponse(
            status_code=status.HTTP_201_CREATED,
            transaction_id=outcome.transaction.id,
            body=outcome.transaction.model_dump(mode="json"),
        )

    if idempotency_key is None:
        return _respond(await execute(), replayed=False)

    request_hash = fingerprint(await request.body())
    result = await state.idempotency.run(idempotency_key, request_hash, execute)
    return _respond(result.response, replayed=result.replayed)


def _respond(stored: StoredResponse, *, replayed: bool) -> JSONResponse:
    """Render a response. A replayed `201` is a `200`: the resource already existed."""
    code = stored.status_code
    if replayed and code == status.HTTP_201_CREATED:
        code = status.HTTP_200_OK
    return JSONResponse(stored.body, status_code=code)
