"""A single application error family that maps itself to an HTTP response.

Handlers raise these and one exception handler does the mapping, which keeps
status-code policy in exactly one place. Full detail is logged only on 5xx, so
internals (connection strings, driver messages) never reach a client.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here - `raise NotFound()` from four frames deep needs no plumbing in between,
which is the whole reason handlers can stay this short.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "CacheError",
    "DatabaseError",
    "NotFound",
    "RateLimited",
    "Unauthorized",
    "app_error_handler",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(
        self, message: str | None = None, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message
        self.headers = headers
        """Extra response headers. The rate limiter uses this to keep governor-style
        `retry-after` / `x-ratelimit-*` hints on a 429 while still returning the
        same JSON envelope as every other error."""


class NotFound(AppError):
    """The slug does not resolve to a link."""

    status_code = 404
    message = "not found"


class Unauthorized(AppError):
    """A write/stats request lacked a valid API key."""

    status_code = 401
    message = "unauthorized"


class BadRequest(AppError):
    """Malformed input - a bad URL, an illegal custom slug, a taken slug."""

    status_code = 400
    message = "bad request"


class RateLimited(AppError):
    """The caller's API key has spent its burst budget."""

    status_code = 429
    message = "too many requests"


class DatabaseError(AppError):
    """Postgres failed. 500: the caller can do nothing about it."""

    status_code = 500
    message = "internal server error"


class CacheError(AppError):
    """Redis failed in a place we chose not to degrade.

    Rare by design - the redirect path *catches* cache failures and falls back
    to Postgres (SPEC V2, "degrade, not die"). This exists for the paths where
    that fallback does not apply.
    """

    status_code = 500
    message = "internal server error"


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
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
