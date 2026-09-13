"""The control-plane API's errors → HTTP.

These are the errors of the *control-plane API*: submit a job, inspect it. The
worker side doesn't answer HTTP — a task that fails becomes a retry or a dead task,
settled inside the worker loop (`worker.py`), never rendered here.

## Exceptions, not a result type

The Rust returned `Result<T, AppError>` and threaded `?` through every handler.
Python's `raise` already unwinds to the one place that renders it — the handlers
registered below — so the store's methods can be annotated `-> JobView | None` and
mean it. Carrying `Result` over would be Rust-in-Python.

## Keep internals out of the message

A 5xx carries whatever went wrong inside: an ffmpeg stderr that embeds the whole
command line (source paths included), a Postgres message naming a table. So the
rule is the Rust one: log the full error server-side, and on any 5xx the client
gets only `"internal server error"`. A 4xx message *is* for the client — which
means it must not echo what the filesystem said about a path either.
"""

from __future__ import annotations

import asyncpg
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequestError",
    "NotFoundError",
    "TranscodeToolError",
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
    """No job (or task) with that id."""

    status_code = 404
    message = "not found"


class BadRequestError(AppError):
    """The request failed validation the framework can't do for you: a source path
    that escapes `WORK_DIR`, an out-of-bounds ladder."""

    status_code = 400
    message = "bad request"


class TranscodeToolError(AppError):
    """An `ffmpeg` / `ffprobe` invocation failed — it could not be spawned, or it
    exited non-zero.

    Carries the tool's stderr, so the failure is diagnosable in the log and in the
    task's `last_error`. A 500, so that detail never reaches an HTTP client.
    """

    status_code = 500
    message = "transcode tool failed"


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
    full, never shown: a constraint name is a map of your schema."""
    logger.error("database error", error=str(exc), kind=type(exc).__name__)
    return _render(500, _INTERNAL)


async def _not_implemented(_request: Request, exc: Exception) -> JSONResponse:
    """Render an unbuilt vertical as `501 Not Implemented`, naming the todo.

    Without this, a handler that reaches an unbuilt store method is a bare 500 and
    a traceback. With it, `curl` shows which vertical you are on. 501 is also simply
    the correct status: the server does not (yet) support what the request needs.
    """
    logger.warning("reached an unbuilt path", todo=str(exc))
    return JSONResponse(status_code=501, content={"error": "not implemented", "todo": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    """Register the error → HTTP mapping and the scaffold's 501."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(asyncpg.PostgresError, _database_error)
    app.add_exception_handler(asyncpg.InterfaceError, _database_error)
    app.add_exception_handler(NotImplementedError, _not_implemented)
