"""A single application error family that maps itself to an HTTP response.

Note the split this project makes. *HTTP* errors — a rejected `GET /ws` upgrade,
a bad admin request — use these types and become status codes. Errors that
happen *after* the socket is open are not HTTP any more: they become an `error`
frame on the socket or a WebSocket close, handled in the connection loop, not
here. Once you have returned `101 Switching Protocols` there is no status code
left to send.

Python shape note: a small class hierarchy with the status code as a class
attribute, and handlers `raise` rather than return. Raising beats a returned
result type here — a validation failure several frames deep needs no plumbing
through every caller in between.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "BusError",
    "StoreError",
    "Unauthorized",
    "Unavailable",
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
    """The client sent something we cannot act on."""

    status_code = 400
    message = "bad request"


class Unauthorized(AppError):
    """The upgrade request failed auth (missing or invalid token)."""

    status_code = 401
    message = "unauthorized"


class Unavailable(AppError):
    """A feature is disabled by configuration — e.g. the `/admin` roster API
    when `DATABASE_URL` is unset. The pub/sub core runs without it."""

    status_code = 503
    message = "unavailable"


class BusError(AppError):
    """The cross-node bus (Redis) failed."""

    status_code = 500
    message = "bus error"


class StoreError(AppError):
    """A directory (admin-panel roster) database query failed."""

    status_code = 500
    message = "store error"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Takes `Exception` rather than `AppError` because that is the signature
    Starlette's handler registry is typed against; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        # Full detail to the log, a generic string to the caller — a 5xx must
        # never leak connection strings or driver internals to a client.
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"error": AppError.message}
    else:
        body = {"error": exc.message}
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
