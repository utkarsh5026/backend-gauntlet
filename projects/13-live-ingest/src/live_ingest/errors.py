"""One error family for a process whose two planes fail by opposite rules.

* **The delivery plane is HTTP.** A viewer asking for a stream that is not on
  air, or a part that has already fallen out of the window, gets a status code
  and a small JSON body — `404`, `503` while a stream is still coming up, `400`
  for a malformed request. `app_error_handler` renders these.
* **The ingest plane is raw TCP.** A publisher that sends a bad chunk header,
  an absurd length, malformed AMF0 or an unauthorized stream key does not get a
  status code — there is no such thing on an RTMP socket. It gets its
  connection **closed**, and nothing else on the server notices. Every such
  failure is a `ProtocolError`, and the session loop in `ingest.py` has exactly
  one `except ProtocolError` around a connection's life.

## Why exceptions rather than a result type

Rust returned `Result<Message, AppError>` from the chunk reader because it had
no other choice. Reading one chunk is basic header → csid escape → fmt-sized
message header → extended timestamp → payload, and every step can run out of
bytes or declare something absurd. Threading an error value back up that walk
turns each bounds check into control flow. In Python, `raise` is how that walk
unwinds, and the one `except` at the session boundary is where a hostile
publisher is *supposed* to be handled.

So the vertical modules raise; they do not return errors.

## The Python failure mode that is worse than a Rust panic

`payload[4:8]` on a three-byte buffer does not raise. It returns three bytes,
`int.from_bytes` turns them into a number, and the bug surfaces as a message
length that is off by a factor of 256 four calls later. That is why
`TruncatedError` exists and why every parser here checks lengths *before* it
slices. On the socket, `StreamReader.readexactly` does the check for you — it
raises `asyncio.IncompleteReadError` instead of returning short — but the AMF0
and FLV parsers work on `bytes` already in memory, where nothing checks for you.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequestError",
    "HandshakeError",
    "MalformedError",
    "NotFoundError",
    "NotReadyError",
    "OversizedError",
    "PackagingError",
    "ProtocolError",
    "StateError",
    "TruncatedError",
    "UnauthorizedError",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this server raises on purpose.

    A subclass sets `status_code` (how HTTP renders it), `client_safe` (whether
    its message may be shown to a caller) and optionally `headers`.
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False → the detail goes to the log and the caller gets a flat message.
    client_safe: bool = True
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


# --------------------------------------------------------------------------- #
# The delivery plane — rendered as HTTP
# --------------------------------------------------------------------------- #


class NotFoundError(AppError):
    """No stream on air for that key, or the msn/part is outside the live window.

    Both halves of the SPEC's "outside the live window gets a clean 404": a part
    already evicted from the front of the ring, and one that does not exist yet
    and is not the preload hint. A 404 is the *truthful* answer to both — the
    bytes are not here, and a player that asked for them should move on.
    """

    status_code = 404
    message = "not found"


class NotReadyError(AppError):
    """The stream exists but has not produced what was asked for yet.

    A publisher that has connected but whose first keyframe has not arrived has
    no init segment. That is not *missing* (404, give up) — it is *coming*, so
    `503` with `Retry-After` tells a player to try again shortly.
    """

    status_code = 503
    message = "stream not ready"
    headers = {"Retry-After": "1"}


class BadRequestError(AppError):
    """A malformed request: an unsafe path segment, unparseable reload params."""

    status_code = 400
    message = "bad request"


class PackagingError(AppError):
    """Building an init, part or segment failed.

    Not client-safe: the detail is box-layout internals, useful in a log and
    meaningless to a player.
    """

    status_code = 500
    message = "packaging failed"
    client_safe = False


# --------------------------------------------------------------------------- #
# The ingest plane — costs one RTMP session, never the server
# --------------------------------------------------------------------------- #


class ProtocolError(AppError):
    """Base for anything that ends one publisher's connection and nothing more.

    The session boundary catches *this*; one `except` covers every way a
    publisher can be wrong. Never client-safe: these never reach an HTTP
    caller, and their detail is a map of how far into your parser a prober got.

    Note what is deliberately **not** a subclass: `NotImplementedError`. A
    vertical you have not written yet is not the publisher's fault, so it is not
    swallowed as one — it ends that connection loudly, with its own log line,
    exactly like the Rust scaffold's `todo!()` panic did.
    """

    status_code = 400
    message = "rtmp protocol error"
    client_safe = False


class HandshakeError(ProtocolError):
    """C0 was not version 3, or C2 did not echo the S1 random block."""

    message = "rtmp handshake failed"


class TruncatedError(ProtocolError):
    """A structure was shorter than its layout requires.

    On the socket this is the peer hanging up mid-chunk (`readexactly` raising
    `IncompleteReadError`); in memory it is an AMF0 string whose declared length
    runs past the end of the payload. Either way: stop, do not guess.
    """

    message = "truncated input"

    def __init__(self, *, need: int, got: int) -> None:
        super().__init__(f"truncated: need {need} bytes, got {got}")
        self.need = need
        self.got = got


class MalformedError(ProtocolError):
    """Well-sized but internally inconsistent.

    A fmt 1/2/3 chunk on a chunk stream id that has no prior header to inherit
    from, a new message starting on a csid whose previous one is incomplete, an
    unsupported AMF0 marker, an object without its `00 00 09` terminator, an FLV
    tag whose packet type is not one you handle.
    """

    message = "malformed input"


class OversizedError(ProtocolError):
    """A peer-declared size exceeded the bound this server allows.

    The declared message length, a Set Chunk Size, an AMF0 string. Raised
    *before* anything is allocated for it — the whole value of the check.
    """

    message = "declared size exceeds limit"

    def __init__(self, *, what: str, declared: int, limit: int) -> None:
        super().__init__(f"{what} {declared} exceeds limit {limit}")
        self.what = what
        self.declared = declared
        self.limit = limit


class StateError(ProtocolError):
    """A message arrived in a session state where it is not legal.

    Media before `publish`, `publish` before `createStream`, a second `connect`.
    Whether a given case *raises* this or is ignored is V2's documented
    decision; this is the type to raise when the answer is "reject".
    """

    message = "message not valid in this session state"


class UnauthorizedError(ProtocolError):
    """A `publish` to a stream key the registry does not authorize.

    The message never contains the key. It is a credential, and exception
    messages end up in logs.
    """

    message = "publish refused: stream key not authorized"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` onto a JSON response.

    Typed against `Exception` because that is the signature Starlette's handler
    registry expects; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if exc.status_code >= 500:
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)

    body = exc.message if exc.client_safe else AppError.message
    return JSONResponse(status_code=exc.status_code, content={"error": body}, headers=exc.headers)


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """A path or query parameter that failed validation is a `400`, not a `422`.

    FastAPI's default for `seg/abc.m4s` or `_HLS_msn=-1` is a 422 with the whole
    pydantic error tree in the body. A player cannot act on that, and the tree
    echoes attacker-chosen input back. The SPEC's rule for the HLS side is a
    clean, boring answer: malformed is `400`, unknown is `404`.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    return JSONResponse(status_code=400, content={"error": BadRequestError.message})


def install_error_handlers(app: FastAPI) -> None:
    """Register the error → HTTP mapping on the delivery plane."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
