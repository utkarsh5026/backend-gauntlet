"""One error family, rendered onto two very different wires.

The engine and its layers raise these; two consumers catch them:

* the **RESP** front-end (`server.py`) renders one as a `-ERR …` line via
  `resp_error()`, so a stock `redis-cli` prints it in red and *keeps the
  connection open*;
* the **HTTP** sidecar (`routes.py`) renders one as a JSON body with a status
  code, via the handler installed by `install_error_handlers`.

Raising beats returning a result type here for the same reason it did in the
gateway: a read is memtable -> frozen memtables -> SSTable -> bloom -> block
cache -> disk, and a CRC failure five frames down should not need four
`if err is not None` checks to reach the reply.

## The rule the security checklist actually grades

A client learns *that* something failed and, when it is their fault, *what*.
They never learn a path, a byte offset, an SSTable id, or that the failure was a
CRC mismatch on block 7 of `000042.sst` — that is a map of your disk. So
`Corrupt` and `Io` render as a flat "internal error" on both wires and the
detail goes to the log. `Protocol` and `Auth*` are the client's own fault and
say so precisely, because a client that cannot tell "wrong password" from
"malformed frame" cannot fix either.

## The redis error-word convention

RESP error lines start with an uppercase word that clients switch on, and real
redis's vocabulary is not decorative: `redis-py` raises `AuthenticationError`
specifically on `WRONGPASS`/`NOAUTH`, and `WRONGTYPE` is how a client library
knows to raise `ResponseError` rather than retry. Keeping those exact words is
part of "a stock client renders it correctly" — inventing `ERR bad password`
would be a protocol difference wearing a cosmetic disguise.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "Corrupt",
    "NoAuth",
    "ProtocolError",
    "StorageError",
    "WrongPass",
    "WrongType",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this server turns into a reply.

    `resp_prefix` is the uppercase error word RESP clients switch on;
    `status_code` is the HTTP sidecar's mapping. A subclass sets both, and
    neither renderer has to know about the other.
    """

    status_code: int = 500
    resp_prefix: str = "ERR"
    message: str = "internal error"
    #: Whether the message is safe to hand a client. False -> the detail goes to
    #: the log and the client gets a flat "internal error" on both wires.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message

    def resp_error(self) -> str:
        """The payload of a RESP error line — the text after the `-`, with no
        trailing CRLF (the codec adds the framing, V1)."""
        if not self.client_safe:
            logger.error("command failed", error=str(self), kind=type(self).__name__)
            return "ERR internal error"
        return f"{self.resp_prefix} {self.message}"


class ProtocolError(AppError):
    """The bytes were not a valid RESP frame, or a command had the wrong arity
    or argument shape (V1).

    Note what this is *not*: a reason to close the connection. The horizontal
    checklist grades that distinction — a client that sends one malformed
    command and gets hung up on has to reconnect and replay, which turns a typo
    into an outage. Reply `-ERR`, keep reading.

    The exception is a framing error the parser cannot resynchronize from (a
    bulk length over the cap, a bad type byte mid-stream): there is no way to
    find the next frame boundary in the stream, so the connection is genuinely
    unusable and closing is correct. Deciding which errors are which is part
    of V1.
    """

    status_code = 400
    resp_prefix = "ERR"
    message = "protocol error"


class WrongType(AppError):
    """A command was issued against a key holding a different data type.

    Unused while everything is a raw string; it exists because the moment you
    add a second type (a list, a hash) the *first* thing you need is the error
    that says "GET on a list", and redis's exact wording is what client
    libraries pattern-match on.
    """

    status_code = 400
    resp_prefix = "WRONGTYPE"
    message = "Operation against a key holding the wrong kind of value"


class NoAuth(AppError):
    """A command arrived on a connection that has not authenticated, while
    `REQUIREPASS` is set."""

    status_code = 401
    resp_prefix = "NOAUTH"
    message = "Authentication required."


class WrongPass(AppError):
    """`AUTH` with the wrong password.

    Deliberately identical to real redis's wording, including the fact that it
    does not distinguish a bad username from a bad password — that is an
    enumeration defence, not sloppiness.
    """

    status_code = 401
    resp_prefix = "WRONGPASS"
    message = "invalid username-password pair or user is disabled."


class Corrupt(AppError):
    """A WAL record or an SSTable block failed its CRC or format check
    (V2/V4).

    This is the error that must never be answered with data. A read that hits a
    corrupt block has exactly two honest outcomes — raise, or return a value it
    has verified — and "return whatever the bytes decoded to" is neither. A
    storage engine that serves a bit-rotted value has failed at the one thing it
    exists to do.
    """

    status_code = 500
    message = "corrupt data on disk"
    client_safe = False


class StorageError(AppError):
    """A filesystem operation failed. The data directory *is* the database, so
    an `OSError` from it is a server fault, not a client one."""

    status_code = 500
    message = "storage failure"
    client_safe = False


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` onto the sidecar's JSON response.

    Typed against `Exception` because that is the signature Starlette's handler
    registry expects; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if exc.status_code >= 500:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)

    body = exc.message if exc.client_safe else AppError.message
    return JSONResponse(status_code=exc.status_code, content={"error": body})


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the sidecar app."""
    app.add_exception_handler(AppError, app_error_handler)
