"""The platform's error family → HTTP.

This capstone has several planes with different failure semantics, but they all
surface over HTTP, so one family covers them:

* **Control plane** (ingest webhook, session lifecycle): an unknown stream key is
  rejected, a cap or an illegal transition is a conflict.
* **Playback / edge** (`…/*.m3u8`, segments): a miss the origin cannot satisfy is
  a 404; an origin failure is a 502 and an origin *timeout* a 504. A viewer gets
  a status, never a hung connection.
* **Chat** (WebSocket): a rejection before the handshake completes maps to a
  status; after it, failures live on the socket, not here.

## Why exceptions rather than a result type

The Rust returned `Result<T, AppError>` and threaded `?` through every handler.
Python's `raise` already unwinds to the one place that renders it — the handler
registered below — so the vertical modules raise and their annotations can say
`StreamSession` and mean it. Carrying `Result` over would be Rust-in-Python.

## Keep secrets out of the message

A stream key is the broadcaster's ingest secret *and* the playback URL slug.
Messages here reach clients, so no error ever interpolates one. `client_safe`
is the second line of defence: an error marked unsafe has its detail logged and
the caller gets the class's flat message instead.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "ConflictError",
    "DependencyError",
    "NotFoundError",
    "RejectedError",
    "UpstreamError",
    "UpstreamTimeoutError",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this platform raises on purpose.

    A subclass sets `status_code` and `client_safe`; nothing else needs to know
    about either.
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False → the detail goes to the log and the caller gets the class's flat message.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotFoundError(AppError):
    """The stream, rendition or segment the request names does not exist."""

    status_code = 404
    message = "not found"


class ConflictError(AppError):
    """The request conflicts with current state: `max_streams` is reached, or a
    state transition is not legal (e.g. `LIVE` straight from `OFFLINE`)."""

    status_code = 409
    message = "conflict"


class RejectedError(AppError):
    """Refused before doing any work: an unregistered stream key on the ingest
    webhook, an invalid or expired playback token, a malformed name in a path.

    400 mirrors the Rust mapping. Whether an unknown key should instead be a 403
    or a 404 is worth a sentence in the design doc — each one tells a prober
    something different about which keys exist."""

    status_code = 400
    message = "rejected"


class UpstreamError(AppError):
    """The edge asked the packager origin for bytes and it failed.

    Not client-safe: the detail is an internal hostname and a status the viewer
    has no use for."""

    status_code = 502
    message = "origin unavailable"
    client_safe = False


class UpstreamTimeoutError(UpstreamError):
    """The origin fill ran past its deadline. A subclass, so code that handles
    "the origin failed" catches both without caring which."""

    status_code = 504
    message = "origin timed out"


class DependencyError(AppError):
    """Postgres, Redis or NATS was unreachable. Degrade where you can; surface
    503 where you cannot, so a load balancer routes around this pod."""

    status_code = 503
    message = "dependency unavailable"
    client_safe = False


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    """Render an `AppError`. Typed against `Exception` because that is the
    signature Starlette's handler registry expects; the narrowing happens here."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)
    body = exc.message if exc.client_safe else type(exc).message
    return JSONResponse(status_code=exc.status_code, content={"error": body})


async def _not_implemented(_request: Request, exc: Exception) -> JSONResponse:
    """Render an unbuilt vertical as `501 Not Implemented`, naming the todo.

    Without this, a scaffold handler that reaches `raise NotImplementedError`
    is a bare 500 and a traceback in the log. With it, `curl` shows you which
    vertical you are on. 501 is also simply the correct status: the server does
    not (yet) support the functionality the request needs.
    """
    logger.warning("reached an unbuilt path", todo=str(exc))
    return JSONResponse(status_code=501, content={"error": "not implemented", "todo": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    """Register the `AppError` → HTTP mapping and the scaffold's 501."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(NotImplementedError, _not_implemented)
