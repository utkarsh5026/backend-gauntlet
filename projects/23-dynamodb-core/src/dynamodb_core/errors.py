"""DynamoDB's named exceptions, mapped to HTTP responses.

Handlers raise these; one handler does the mapping, so status-code policy lives in
exactly one place. The names match the real service so the mental model transfers —
if you have ever retried a `ProvisionedThroughputExceededException` in production,
this is the thing that was throwing it.

Each carries `retryable`, which is the distinction that actually matters to a
client: a throttle means *come back shortly*, a failed condition means *your
assumption was wrong, retrying unchanged will fail again*. A client that retries
the second one is a client in an infinite loop.

Note: the real service returns HTTP 400 for nearly all of these and distinguishes
them only by the error name in the body. This scaffold uses HTTP-meaningful codes
instead (409 for a conflict, 429 for a throttle); if you'd rather mirror AWS
exactly, that's a legitimate change — record whichever you pick in the design doc.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "ConditionalCheckFailed",
    "ItemCollectionSizeLimitExceeded",
    "ProvisionedThroughputExceeded",
    "ResourceNotFound",
    "TransactionCanceled",
    "ValidationError",
    "app_error_handler",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    error_code: str = "InternalServerError"
    message: str = "internal server error"
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class ValidationError(AppError):
    """Malformed request: missing key attribute, illegal key type, bad expression."""

    status_code = 400
    error_code = "ValidationException"
    message = "invalid request"


class ResourceNotFound(AppError):
    """No such table or index."""

    status_code = 404
    error_code = "ResourceNotFoundException"
    message = "requested resource not found"


class ConditionalCheckFailed(AppError):
    """A ConditionExpression evaluated false (V3).

    Deliberately **not** retryable: the write was correctly refused, and the caller
    must re-read and decide again.
    """

    status_code = 409
    error_code = "ConditionalCheckFailedException"
    message = "the conditional request failed"


class ProvisionedThroughputExceeded(AppError):
    """Capacity exhausted (V4) — the one clients are expected to back off and retry."""

    status_code = 429
    error_code = "ProvisionedThroughputExceededException"
    message = "throughput exceeds the current capacity for this table or partition"
    retryable = True


class TransactionCanceled(AppError):
    """A transaction leg failed, so none of it applied (V3)."""

    status_code = 409
    error_code = "TransactionCanceledException"
    message = "transaction cancelled"


class ItemCollectionSizeLimitExceeded(AppError):
    """The item exceeds the configured per-item cap (V1)."""

    status_code = 413
    error_code = "ItemCollectionSizeLimitExceededException"
    message = "item size exceeds the maximum allowed"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to a DynamoDB-shaped error body."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"__type": AppError.error_code, "message": AppError.message}
    else:
        body = {"__type": exc.error_code, "message": exc.message}
    headers = {"x-amzn-errortype": exc.error_code}
    if exc.retryable:
        # Tell the client it may retry, so a correct backoff needs no lookup table.
        headers["retry-after"] = "1"
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
