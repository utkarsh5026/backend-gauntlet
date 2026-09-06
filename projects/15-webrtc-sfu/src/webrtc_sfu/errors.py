"""One error family for a process running two planes with opposite failure rules.

* **The media plane is UDP.** A malformed STUN/RTP/RTCP datagram is *dropped*,
  never turned into a status code — an open UDP port takes bytes from anyone, so
  every parse failure has to cost at most one datagram. The pump wraps each
  dispatch in one `except MediaError`, logs it at debug, and goes back to
  receiving. There is nobody to answer.
* **The signaling plane is HTTP.** Those failures (unknown publisher, room full,
  malformed body) *do* map to a status code, rendered by the handler
  `install_error_handlers` registers.

## Why exceptions rather than a result type

Rust returned `Result<T, SfuError>` because it had no other choice. Python does,
and carrying that shape over would be the anti-pattern this conversion exists to
avoid. Parsing one STUN message is header -> cookie -> class -> txid -> a walk
over attribute TLVs, each of which can overrun the buffer; threading `if err is
not None` down that walk turns a bounds check into a control-flow problem.
`raise` is how Python unwinds, and the `try/except` at the pump's dispatch is
where a hostile datagram is *supposed* to be handled — one `except` around the
datagram, not a return-value check per field.

The vertical modules therefore raise; they do not return errors. `parse` returns
a `StunMessage` or raises — which is also why its type annotation can say
`StunMessage` and mean it.

## The rule the security checklist grades

The media-plane errors are not client-safe, and here that is not a formality: a
`Truncated(need=20, got=7)` handed back over HTTP tells a scanner exactly how
far it got into your parser, and an `IntegrityError` that names the ufrag it
failed against is a credential oracle. They render as a flat "bad request" and
put the detail in the log. `NotFound` and `Rejected` say exactly what was wrong,
because a client that cannot tell "no such publisher" from "room full" cannot
fix either.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadMagicError",
    "IntegrityError",
    "MalformedError",
    "MediaError",
    "NotFoundError",
    "RejectedError",
    "TruncatedError",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this SFU raises on purpose.

    A subclass sets `status_code` (how signaling renders it) and `client_safe`
    (whether its message may be shown to a caller). Nothing else needs to know
    about either.
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False -> the detail goes to the log and the caller gets a flat message.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class MediaError(AppError):
    """Base for anything that costs one datagram and nothing more.

    The pump catches *this* — one `except MediaError` covers every way a
    datagram can be wrong, which is the point of giving them a common base. Not
    client-safe: these never reach an HTTP caller, and the detail is a map of
    how far into the parser a prober got.
    """

    status_code = 400
    message = "bad datagram"
    client_safe = False


class TruncatedError(MediaError):
    """A datagram (or a field inside it) was shorter than its layout requires.

    The single most common shape of hostile input on an open UDP port, and the
    reason every parser in this project range-checks before it slices. In Python
    the failure mode is quieter than a Rust panic and worse for it: `buf[4:8]` on
    a three-byte buffer does not raise, it returns three bytes, and the bug
    surfaces four layers up as a nonsensical length. Check first.
    """

    message = "truncated datagram"

    def __init__(self, *, need: int, got: int) -> None:
        super().__init__(f"truncated: need at least {need} bytes, got {got}")
        self.need = need
        self.got = got


class BadMagicError(MediaError):
    """A STUN message whose cookie was not `0x2112A442`, or an RTP packet whose
    version bits were not `2` — i.e. not the protocol we thought it was."""

    message = "bad magic/version"


class MalformedError(MediaError):
    """Well-sized but internally inconsistent: a STUN attribute length that
    overruns the message, an RTCP length word that does not fit, a NACK FCI
    count out of range."""

    message = "malformed datagram"


class IntegrityError(MediaError):
    """A STUN MESSAGE-INTEGRITY or FINGERPRINT that did not verify.

    The check came from something that does not hold the ICE `pwd`. Dropped, and
    — the criterion that matters — it must never nominate a path: authentication
    failing is precisely the case where doing *nothing* is the correct handling.
    """

    message = "integrity check failed"


class NotFoundError(AppError):
    """Signaling named a room, publisher or peer that does not exist."""

    status_code = 404
    message = "not found"


class RejectedError(AppError):
    """A cap was hit (`MAX_ROOMS`, `MAX_PEERS_PER_ROOM`) or the request was
    otherwise refused.

    409 rather than 400 on purpose: the request was well-formed, the *server* is
    full. A client that retries a 400 is broken; a client that retries this one
    is doing the right thing.
    """

    status_code = 409
    message = "rejected"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` onto the signaling plane's JSON response.

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
    """Register the AppError -> HTTP mapping on the signaling plane."""
    app.add_exception_handler(AppError, app_error_handler)
