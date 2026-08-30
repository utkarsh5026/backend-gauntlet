"""A single application error family that maps itself to an HTTP response.

These are the errors of the *ingest + query API*. The consumer pipeline (rollup
-> sink) doesn't return HTTP — a point that fails to parse is rejected and
counted (see `parse.py`), and a sink write that fails is retried by redelivery
(at-least-once, see `sink.py`), handled in the pipeline loop.

Handlers raise these and one exception handler does the mapping, which keeps
status-code policy in exactly one place. The handler logs the full error only on
5xx, so internals (broker URLs, ClickHouse errors) never reach a client.

Python shape note: this is a small class hierarchy with the status code as a
class attribute, and handlers `raise` rather than return. Raising beats a
returned result type here — a parse failure four frames deep needs no plumbing
through every caller in between.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "BrokerUnavailable",
    "NotFound",
    "StoreError",
    "StoreUnavailable",
    "Unauthorized",
    "app_error_handler",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class BadRequest(AppError):
    """The body or query failed validation — malformed line, bad range,
    oversized payload, too many tags, absurd timestamp."""

    status_code = 400
    message = "bad request"


class Unauthorized(AppError):
    """Ingest or query arrived without a valid API key (security horizontal).

    Unused by the scaffold: wiring the check is yours (see `routes.py`). An open
    `/ingest` lets anyone forge metrics or blow up your cardinality.
    """

    status_code = 401
    message = "unauthorized"


class NotFound(AppError):
    """Nothing matched — e.g. a query range with no rollups in it."""

    status_code = 404
    message = "not found"


class BrokerUnavailable(AppError):
    """Publishing to the durable stream failed (broker down, timeout).

    503, not 500, and the difference is a real decision: ingest is a *retryable*
    write. The client has the points in hand and a 503 (plus `Retry-After`) tells
    a well-behaved sender to hold them and try again, where a 500 says "this
    request is broken, don't bother". The Rust scaffold mapped both to 500 —
    fixing that is a freebie on the way past.
    """

    status_code = 503
    message = "broker unavailable"


class StoreError(AppError):
    """A ClickHouse read or write failed."""

    status_code = 500
    message = "store error"


class StoreUnavailable(StoreError):
    """No connection to ClickHouse at all — the store was down at startup.

    503 rather than its parent's 500, for the same reason as
    `BrokerUnavailable`: nothing is wrong with the request, and a caller that
    backs off and retries will succeed. Distinguishing "your query broke" from
    "come back later" is the whole value of having an error taxonomy.
    """

    status_code = 503
    message = "store unavailable"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Takes `Exception` rather than `AppError` because that is the signature
    Starlette's handler registry is typed against; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        # Full detail to the log, a generic string to the caller.
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"error": AppError.message}
    else:
        body = {"error": exc.message}
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
