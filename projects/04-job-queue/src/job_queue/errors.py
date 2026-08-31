"""A single application error family that maps itself to an HTTP response.

These are the errors of the *producer/admin API* (enqueue, status, DLQ). The
worker side doesn't return HTTP — a job that fails becomes a retry or a
dead-letter (see `retry`), handled in the worker loop, not here.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here — `raise NotFound()` from four frames deep needs no plumbing in between.

Full detail is logged only on 5xx, so internals (connection strings, driver
messages) never reach a client.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "BodyTooLarge",
    "DatabaseError",
    "NotFound",
    "Unauthorized",
    "app_error_handler",
    "install_error_handlers",
    "validation_error_handler",
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


class NotFound(AppError):
    """No job with that id (or no *dead* job with that id, on requeue)."""

    status_code = 404
    message = "not found"


class Unauthorized(AppError):
    """Missing or invalid enqueue credential."""

    status_code = 401
    message = "unauthorized"


class BadRequest(AppError):
    """The request body failed validation (bad/oversized payload, etc.)."""

    status_code = 400
    message = "bad request"


class BodyTooLarge(AppError):
    """The request body exceeded the coarse pre-parse guard in `routes`."""

    status_code = 413
    message = "request body too large"


class DatabaseError(AppError):
    """A database / queue operation failed. 500: the caller can do nothing about it."""

    status_code = 500
    message = "internal server error"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an :class:`AppError` to its response.

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


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Turn a body that failed :class:`~job_queue.job.NewJob`'s caps into a **400**.

    FastAPI's default for a `RequestValidationError` is 422. The SPEC's API
    criterion asks for `400` on a malformed body, and a caller should not have to
    parse two different error envelopes depending on which layer rejected them —
    so this re-shapes it into the same `{"error": …}` body every other failure uses.

    The detail string is pydantic's own (which field, which bound) and is safe to
    return: it describes the *request*, never the server.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registry invariant
        raise exc
    reasons = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
        for err in exc.errors()
    )
    return JSONResponse(status_code=400, content={"error": f"bad request: {reasons}"})


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
