"""One error family for a process whose two planes fail by opposite rules.

* **The media plane is UDP.** A malformed RTP or RTCP datagram is *dropped*,
  never turned into a status code — there is nobody to answer, and an open UDP
  port takes bytes from anyone. Every parse failure has to cost at most one
  datagram, which is why the session loop wraps each dispatch in a single
  `except MediaError` and goes back to receiving.
* **The admin plane is HTTP.** It is trivially infallible (`/healthz`,
  `/status`, `/metrics`), but the handler is registered anyway so that anything
  that *does* go wrong renders as JSON rather than a bare 500 from Starlette.

## Why exceptions rather than a result type

Rust returned `Result<T, TransportError>` because it had no other choice.
Python does, and carrying that shape over would be the anti-pattern this
conversion exists to avoid. Parsing one RTP packet is version → CSRC count →
marker/PT → sequence → timestamp → SSRC → a walk over CSRC words, each of which
can overrun the buffer; threading `if err is not None` down that walk turns a
bounds check into a control-flow problem. `raise` is how Python unwinds, and
the `try/except` at the session loop is where a hostile datagram is *supposed*
to be handled — one `except` around the datagram, not a return-value check per
field.

So the vertical modules raise; they do not return errors. `RtpPacket.parse`
returns an `RtpPacket` or raises — which is why its annotation can say
`RtpPacket` and mean it.

## The Python failure mode that is worse than a Rust panic

In Rust, indexing past the end of a slice panics: loud, immediate, at the line
that did it. In Python, `data[4:8]` on a three-byte buffer does not raise. It
returns three bytes. `int.from_bytes` then happily turns them into a number,
and the bug surfaces four layers away as a nonsensical length word or a
sequence number that is off by a factor of 256.

That is why `TruncatedError` exists and why every parser here checks
`len(data)` *before* it slices. The quiet failure is the dangerous one:
`struct.unpack_from` at least raises `struct.error`, but a bare slice will lie
to you all day.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadVersionError",
    "MalformedError",
    "MediaError",
    "OversizedError",
    "TruncatedError",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this transport raises on purpose.

    A subclass sets `status_code` (how the admin plane would render it) and
    `client_safe` (whether its message may be shown to a caller). Nothing else
    needs to know about either.
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False → the detail goes to the log and the caller gets a flat message.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class MediaError(AppError):
    """Base for anything that costs one datagram and nothing more.

    The session loop catches *this* — one `except MediaError` covers every way
    a datagram can be wrong, which is the entire point of giving them a common
    base. Not client-safe: these never reach an HTTP caller, and their detail is
    a map of how far into your parser a prober got.
    """

    status_code = 400
    message = "bad datagram"
    client_safe = False


class TruncatedError(MediaError):
    """A datagram (or a field inside it) was shorter than its layout requires.

    The most common shape of hostile input on an open UDP port, and the reason
    every parser in this project range-checks before it slices. See the module
    docstring on why Python makes this failure quieter, and worse, than Rust
    does.
    """

    message = "truncated datagram"

    def __init__(self, *, need: int, got: int) -> None:
        super().__init__(f"truncated: need at least {need} bytes, got {got}")
        self.need = need
        self.got = got


class BadVersionError(MediaError):
    """An RTP or RTCP packet whose version bits were not `2`.

    Two bits, and they are the cheapest filter you have: on a public port most
    of what arrives is not this protocol at all. Check them first and the rest
    of the parser only ever sees plausible input.
    """

    message = "unsupported version (expected 2)"

    def __init__(self, version: int) -> None:
        super().__init__(f"unsupported version {version} (expected 2)")
        self.version = version


class MalformedError(MediaError):
    """Well-sized but internally inconsistent.

    A CSRC count that claims more words than the datagram holds, an FU-A header
    with neither a start nor an end bit, an RTCP length word that overruns the
    compound packet, a NACK FCI count that does not fit. These are the
    interesting ones: the datagram is long enough to look real, so only the
    internal cross-checks catch it.
    """

    message = "malformed packet"


class OversizedError(MediaError):
    """A configured MTU too small to hold even an RTP header, so packetization
    is impossible.

    A configuration error rather than a wire error, but it surfaces at the same
    place and gets dropped the same way — which is why it lives in this family.
    """

    message = "mtu too small to packetize"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` onto the admin plane's JSON response.

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
    """Register the AppError → HTTP mapping on the admin plane."""
    app.add_exception_handler(AppError, app_error_handler)
