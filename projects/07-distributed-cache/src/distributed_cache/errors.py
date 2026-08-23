"""A single application error family that maps itself to an HTTP response.

Handlers raise these and let one exception handler do the mapping, which keeps
status-code policy in exactly one place. The handler logs the full error only on
5xx, so internals (peer addresses, connection detail) never reach a client.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here — `raise NotFound()` from four frames deep needs no plumbing in between.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "InvalidRequest",
    "NotFound",
    "Unauthorized",
    "Unavailable",
    "Upstream",
    "ValueTooLarge",
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


class NotFound(AppError):
    """The key is not present (or expired) on any replica."""

    status_code = 404
    message = "not found"


class InvalidRequest(AppError):
    """Malformed request — bad key charset/length, unparseable ttl, and so on."""

    status_code = 400
    message = "invalid request"


class ValueTooLarge(AppError):
    """The value exceeds the configured per-entry size cap."""

    status_code = 413
    message = "value too large"


class Unauthorized(AppError):
    """A write/admin request lacked a valid auth token (security horizontal)."""

    status_code = 401
    message = "unauthorized"


class Unavailable(AppError):
    """No live node currently owns this key.

    The cluster may still be converging, or every replica for the key is down.
    Distinct from `NotFound`: the key *might* exist, we just can't reach an owner
    right now — so the client may retry.
    """

    status_code = 503
    message = "no owner available for key"


class Upstream(AppError):
    """Forwarding a request to a peer replica failed (network, timeout, 5xx)."""

    status_code = 502
    message = "peer request failed"


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
