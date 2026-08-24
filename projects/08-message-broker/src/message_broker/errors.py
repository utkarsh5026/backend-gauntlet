"""A single application error family that maps itself to an HTTP response.

Handlers raise these and one exception handler does the mapping, which keeps
status-code policy in exactly one place. The handler logs the full error only on
5xx, so internals (absolute paths, io detail — and the log *is* the filesystem
here) never reach a client.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here — `raise UnknownTopic()` from four frames deep needs no plumbing in between.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "CorruptFrame",
    "InvalidRequest",
    "RecordTooLarge",
    "TopicAlreadyExists",
    "Unauthorized",
    "UnknownGroup",
    "UnknownPartition",
    "UnknownTopic",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class UnknownTopic(AppError):
    """No topic with that name."""

    status_code = 404
    message = "unknown topic"


class UnknownPartition(AppError):
    """Partition index out of range for the topic."""

    status_code = 404
    message = "unknown partition"


class UnknownGroup(AppError):
    """No such consumer group / member (V4)."""

    status_code = 404
    message = "unknown group or member"


class TopicAlreadyExists(AppError):
    """Tried to create a topic that already exists.

    409 rather than 400: the request was well-formed, it just lost a race with
    reality. A client that retries a create is *not* wrong, so tell it the truth.
    """

    status_code = 409
    message = "topic already exists"


class InvalidRequest(AppError):
    """Malformed request — bad topic/key name, non-positive partition count, a
    negative offset, and so on."""

    status_code = 400
    message = "invalid request"


class RecordTooLarge(AppError):
    """A record's value exceeded the configured `max_record_bytes` cap."""

    status_code = 413
    message = "record too large"


class Unauthorized(AppError):
    """A produce/admin request lacked the write credential (security horizontal)."""

    status_code = 401
    message = "unauthorized"


class CorruptFrame(AppError):
    """A frame on disk failed its length/CRC check (V1).

    Either real corruption or a torn tail that recovery should have truncated.
    A 5xx on purpose: the client did nothing wrong, and the broker just failed
    its one promise. This must never be answered by returning the bad bytes.
    """

    status_code = 500
    message = "corrupt log frame"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Takes `Exception` rather than `AppError` because that is the signature
    Starlette's handler registry is typed against; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        # Full detail to the log, a generic string to the caller.
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"error": AppError.message}
    else:
        body = {"error": exc.message}
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
