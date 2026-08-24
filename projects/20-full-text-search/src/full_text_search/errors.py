"""A single application error family that maps itself to an HTTP response.

Handlers raise these and one exception handler does the mapping, which keeps
status-code policy in exactly one place. The handler logs full detail only on
5xx, so internals — index paths, io errors, segment offsets — never reach a
client.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here, because `raise CorruptSegment()` from inside a segment reader four frames
below the handler needs no plumbing in between — and that is precisely the path
this error has to travel.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "CorruptSegment",
    "DocumentTooLarge",
    "NotFound",
    "QueryTooBroad",
    "Unauthorized",
    "app_error_handler",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response.

    The default is a 500 with a generic message, so a subclass that forgets to
    set `status_code` fails closed rather than leaking.
    """

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotFound(AppError):
    """No document with that external id (delete or lookup)."""

    status_code = 404
    message = "not found"


class Unauthorized(AppError):
    """Missing or invalid API key on a write/admin route (security horizontal)."""

    status_code = 401
    message = "unauthorized"


class BadRequest(AppError):
    """Malformed request — empty text, a bad external id, a bad shard count."""

    status_code = 400
    message = "bad request"


class DocumentTooLarge(AppError):
    """The document's text exceeded the configured `MAX_DOC_BYTES` cap."""

    status_code = 413
    message = "document too large"


class QueryTooBroad(AppError):
    """A query had more analyzed terms than `MAX_QUERY_TERMS` allows.

    A 400 rather than a 413: the request is small, it is the *fan-out* it would
    cause that is refused.
    """

    status_code = 400
    message = "query too broad"


class CorruptSegment(AppError):
    """A segment on disk failed its integrity or format check (V2).

    Corruption, or a torn tail a clean flush should never have produced. A 500
    because it is the server's own data that is wrong, and — the point of the
    class existing — it is never returned as *data*: a bad segment must surface
    as an error, never as silently wrong postings.
    """

    status_code = 500
    message = "corrupt segment"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Typed against `Exception` rather than `AppError` because that is the
    signature Starlette's handler registry expects; the narrowing happens here.
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
    """Register the AppError → HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
