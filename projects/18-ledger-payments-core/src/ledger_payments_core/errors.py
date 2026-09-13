"""The API's errors → HTTP.

The money path has failure modes plain CRUD doesn't. A transfer can be *rejected*
(insufficient funds, a bad amount or currency), *conflicted* (an idempotency key
reused with a different body, or still in flight), or *too contended* (the
serialization retries ran out). Each maps to a deliberate status: a rejection is a
clean 4xx the client can act on, never a 500.

## Exceptions, not a result type

The Rust returned `Result<T, AppError>` and threaded `?` through every handler.
Python's `raise` already unwinds to the one place that renders it — the handlers
registered below — so the ledger's methods can be annotated `-> Account | None` and
mean it. Carrying `Result` over would be Rust-in-Python.

## Keep internals out of the message

A 5xx carries whatever went wrong inside: a Postgres message naming a constraint, a
Redis error with a host in it. So the rule is the Rust one: log the full error
server-side, and on any 5xx the client gets only `"internal server error"`. A 4xx
message *is* for the client — which means it must never echo an API key or the
webhook secret either.
"""

from __future__ import annotations

import asyncpg
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

__all__ = [
    "AppError",
    "BadRequestError",
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "InsufficientFundsError",
    "NotFoundError",
    "RetriesExhaustedError",
    "UnauthorizedError",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)

_INTERNAL = "internal server error"


class AppError(Exception):
    """Base for every error this service raises on purpose."""

    status_code: int = 500
    message: str = _INTERNAL

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotFoundError(AppError):
    """No such account or transaction."""

    status_code = 404
    message = "not found"


class UnauthorizedError(AppError):
    """A missing or invalid API key. The message never says which."""

    status_code = 401
    message = "unauthorized"


class BadRequestError(AppError):
    """A request the type system let through but the ledger won't accept: a
    non-positive amount, one over the ceiling, a transfer to the same account, a
    currency that doesn't match the account's."""

    status_code = 400
    message = "bad request"


class InsufficientFundsError(AppError):
    """A no-overdraft account can't cover the debit.

    Rejected and *not* retryable — retrying won't make the money appear. `402
    Payment Required` is exactly this case.
    """

    status_code = 402
    message = "insufficient funds"


class IdempotencyConflictError(AppError):
    """An `Idempotency-Key` reused with a *different* request body (V3).

    The key is bound to the request that created it; a mismatch is a client bug to
    surface, not a replay to serve.
    """

    status_code = 409
    message = "idempotency key reused with a different request body"


class IdempotencyInProgressError(AppError):
    """A request with this key is still executing (V3) — one policy for the
    concurrent duplicate. The other is to wait for its result."""

    status_code = 409
    message = "a request with this idempotency key is still in progress"


class RetriesExhaustedError(AppError):
    """Serialization retries ran out under contention (V2).

    Transient, so the client should retry — which is why it's a 4xx and never a
    500. (`409` matches the Rust mapping; `503` + `Retry-After` is the other
    defensible answer. Either way, it's a decision.)
    """

    status_code = 409
    message = "too much contention, retry"


def _render(status_code: int, message: str) -> JSONResponse:
    body = _INTERNAL if status_code >= 500 else message
    return JSONResponse(status_code=status_code, content={"error": body})


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    """Render an `AppError`. Typed against `Exception` because that is the
    signature Starlette's handler registry expects; the narrowing happens here."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)
    return _render(exc.status_code, exc.message)


async def _database_error(_request: Request, exc: Exception) -> JSONResponse:
    """A Postgres failure is a 500 — the Rust `AppError::Db` mapping. Logged in
    full, never shown: a constraint name is a map of your schema.

    Note what should *never* reach here: a serialization conflict. That's V2's
    retry loop's to catch; one escaping as a 500 is the bug the SPEC names.
    """
    logger.error("database error", error=str(exc), kind=type(exc).__name__)
    return _render(500, _INTERNAL)


async def _cache_error(_request: Request, exc: Exception) -> JSONResponse:
    """A Redis failure that escaped. Usually it shouldn't: the cache is an
    optimisation in front of Postgres, so V3 degrades to the durable record rather
    than failing the request. This is the backstop for the genuinely unrecoverable."""
    logger.error("cache error", error=str(exc), kind=type(exc).__name__)
    return _render(500, _INTERNAL)


async def _not_implemented(_request: Request, exc: Exception) -> JSONResponse:
    """Render an unbuilt vertical as `501 Not Implemented`, naming the todo.

    Without this, a handler that reaches an unbuilt function is a bare 500 and a
    traceback. With it, `curl` shows which vertical you are on. 501 is also simply
    the correct status: the server does not (yet) support what the request needs.
    """
    logger.warning("reached an unbuilt path", todo=str(exc))
    return JSONResponse(status_code=501, content={"error": "not implemented", "todo": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    """Register the error → HTTP mapping and the scaffold's 501."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(asyncpg.PostgresError, _database_error)
    app.add_exception_handler(asyncpg.InterfaceError, _database_error)
    app.add_exception_handler(RedisError, _cache_error)
    app.add_exception_handler(NotImplementedError, _not_implemented)
