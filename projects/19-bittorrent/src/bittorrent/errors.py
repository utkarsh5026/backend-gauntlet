"""One error family for a program whose failure modes are mostly not HTTP.

A BitTorrent client's failures are not CRUD failures. A `.torrent` can be
malformed, a tracker can time out or answer with a `failure reason`, a peer can
lie, send garbage, or claim a 4 GiB message. Most of those happen deep inside a
peer session or an announce, where there is no request to respond to — they end
*that* connection, get logged, and the download carries on. Only a few ever
reach a client of the control plane.

So this family has two consumers and they want opposite things:

* the **control plane** (`routes.py`) renders one as a JSON body with a status
  code, via the handler `install_error_handlers` registers;
* a **peer session** (`peer.py`, `seeder.py`) or an **announce** (`tracker.py`)
  catches one, logs it with the peer or tracker it came from, and drops that
  connection — never the process.

## Why exceptions rather than a result type

Rust returned `Result<T, AppError>` because it had to. Python does not, and
dragging that shape over would be the anti-pattern the conversion exists to
avoid: a piece download is tracker -> peer -> handshake -> framing -> block
reassembly -> SHA-1 verify, and a truncated message six frames down should not
need six `if err is not None` checks to reach the log line. `raise` is how
Python unwinds, and `try/except` at the session boundary is where a peer's
misbehaviour is *supposed* to be handled — one `except` around the session, not
a return-value check per frame.

## The rule the security checklist actually grades

A client of the control plane learns *that* something failed and, when it is
their own fault, *what*. They never learn a filesystem path, a peer's address,
or which byte offset of a piece failed its hash — that is a map of your
internals and, worse here than in most projects, a map of the swarm. So
`TrackerError`, `PeerError` and `StorageError` render as a flat "internal server
error" over HTTP and put the detail in the log, while `BadRequest`,
`InvalidTorrent` and `BencodeError` say exactly what was wrong, because a caller
who cannot tell "not a torrent file" from "no such infohash" cannot fix either.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadRequest",
    "BencodeError",
    "InvalidTorrent",
    "NotFound",
    "PeerError",
    "StorageError",
    "TrackerError",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this program raises on purpose.

    A subclass sets `status_code` (how the control plane renders it) and
    `client_safe` (whether its message may be shown to a caller). Nothing else
    has to know about either.
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False -> the detail goes to the log and the caller gets a flat message.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotFound(AppError):
    """No torrent with that infohash is being managed."""

    status_code = 404
    message = "not found"


class BadRequest(AppError):
    """A malformed control-plane request — a non-hex infohash, a body that is
    not a magnet URI."""

    status_code = 400
    message = "bad request"


class BencodeError(AppError):
    """The bytes were not valid bencode (V1).

    A 400 because the overwhelmingly common source is a `.torrent` somebody
    uploaded. When a *tracker reply* fails to decode this is the wrong status —
    the caller in `tracker.py` is expected to catch it and re-raise a
    `TrackerError`, because a tracker sending garbage is not the API caller's
    mistake. Doing that wrapping at the boundary, rather than threading a
    "who am I parsing for?" flag through the codec, is why the codec gets to
    stay a pure function over bytes.
    """

    status_code = 400
    message = "malformed bencode"


class InvalidTorrent(AppError):
    """A `.torrent` or magnet parsed but is internally inconsistent (V2).

    Distinct from `BencodeError` on purpose, and the distinction is the whole
    V2 lesson: well-formed bencode that claims 40 pieces for a 10-piece file is
    *syntactically* perfect and still a lie. Structure and meaning are checked
    in different places and fail differently.
    """

    status_code = 400
    message = "invalid torrent"


class TrackerError(AppError):
    """An announce failed — transport error, timeout, a bad frame, or a
    `failure reason` in the reply (V3).

    Not client-safe: the detail names a tracker URL and often a transaction id,
    and the criterion this serves is that one dead tracker does not sink a
    download. The caller's job is to catch this, count it, and try the next
    tracker.
    """

    status_code = 500
    message = "tracker announce failed"
    client_safe = False


class PeerError(AppError):
    """A peer violated the wire protocol — bad handshake, oversized or garbled
    message, a request for a piece we do not have (V4/V6).

    The most-raised error in the program by a wide margin, and the one whose
    handling *is* a criterion: never trust a peer, and never let one take
    anything down but its own connection. Not client-safe — the message names a
    peer address, which is exactly the kind of thing the SPEC says not to hand
    out.
    """

    status_code = 500
    message = "peer protocol error"
    client_safe = False


class StorageError(AppError):
    """A piece read or write failed. Local, ours, and never the caller's
    fault."""

    status_code = 500
    message = "storage failure"
    client_safe = False


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` onto the control plane's JSON response.

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
    """Register the AppError -> HTTP mapping on the control plane."""
    app.add_exception_handler(AppError, app_error_handler)
