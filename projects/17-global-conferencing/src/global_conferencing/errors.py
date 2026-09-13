"""One error family for three planes with different failure rules.

* **Signaling** (publish/subscribe): an unknown room is a `404`, a full room or a
  hit cap is a `409`, a request this node cannot serve because it is not the
  placement leader is a `421` carrying the leader to retry against.
* **Consensus / placement** (V1, `/cluster/*`): a minority partition **cannot**
  place a new room. That is a `503`, never a silent guess — the refusal is what
  prevents a split room. A peer that does not answer an RPC is `PeerUnreachable`,
  and inside consensus code that is not exceptional at all: it is "no vote from
  that peer this round".
* **The backbone** (V2, UDP): a truncated relay copy, or one from an address that
  is not a known peer, is *dropped*, never turned into a status code — there is
  nobody to answer. Those subclass `MediaError`, which the backbone pump catches
  exactly once.

## Exceptions, not a result type

The Rust returned `Result<T, AppError>` because it had no other choice. Carrying
that shape into Python — returning `(value, error)` or an `Ok | Err` union from
`place_room` — turns every call site into an `if err is not None` ladder and
teaches the wrong habit. Python unwinds with `raise`; FastAPI's exception handler
is where an `AppError` becomes JSON, and the backbone pump's `except MediaError`
is where a hostile datagram is supposed to end.

## Client-safe or not

A `NotFoundError` says exactly what was wrong, because a client that cannot tell
"no such room" from "room full" cannot fix either. A `PeerUnreachableError`'s
detail names an internal control URL — topology a caller has no business seeing —
so it renders flat and the detail goes to the log. Never put the cluster secret,
an ICE password or a peer URL in a client-safe message.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "ConflictError",
    "MediaError",
    "NotFoundError",
    "NotLeaderError",
    "PeerUnreachableError",
    "RejectedError",
    "TruncatedError",
    "UnavailableError",
    "UnknownPeerError",
    "UpstreamError",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this SFU raises on purpose.

    A subclass sets `status_code` (how HTTP renders it) and `client_safe`
    (whether its message may reach a caller).
    """

    status_code: int = 500
    message: str = "internal server error"
    #: False -> the detail goes to the log and the caller gets a flat message.
    client_safe: bool = True

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message

    def body(self) -> dict[str, object]:
        """The JSON body a caller sees."""
        return {"error": self.message if self.client_safe else type(self).message}


class NotFoundError(AppError):
    """The room / publisher / recording named by the request does not exist."""

    status_code = 404
    message = "not found"


class ConflictError(AppError):
    """A cap was hit (`MAX_ROOMS`, `MAX_PEERS_PER_ROOM`, `MAX_RELAY_LINKS`) or a
    state transition is not legal.

    409 rather than 400: the request was well-formed, the *cluster* is full. A
    client that retries this later is doing the right thing.
    """

    status_code = 409
    message = "conflict"


class RejectedError(AppError):
    """Refused before doing any work: a bad cluster credential on an inter-SFU
    RPC, a request that is well-typed but meaningless."""

    status_code = 400
    message = "rejected"


class NotLeaderError(AppError):
    """This node is not the placement leader and did not act.

    `421 Misdirected Request` says "not me" precisely, and the body carries the
    leader this node believes in (if any) so signaling can forward rather than
    fail. Whether a follower forwards on its own or bounces the client is a V1
    decision.
    """

    status_code = 421
    message = "not leader"

    def __init__(self, leader: str | None = None) -> None:
        super().__init__("not leader" if leader is None else f"not leader (leader: {leader})")
        self.leader = leader

    def body(self) -> dict[str, object]:
        return {"error": "not leader", "leader": self.leader}


class UpstreamError(AppError):
    """A peer SFU or relay leg this node reached out to failed or timed out. The
    far region degrades; the near one keeps serving."""

    status_code = 502
    message = "upstream failure"


class PeerUnreachableError(UpstreamError):
    """A `/cluster/*` RPC got no usable answer — refused, timed out, a non-2xx, or
    a reply that did not parse.

    Every one of those collapses into this one type on purpose, so consensus code
    has exactly one thing to catch. Not client-safe: the detail names a peer's
    control URL.
    """

    message = "peer unreachable"
    client_safe = False

    def __init__(self, region: str, detail: str) -> None:
        super().__init__(f"peer {region}: {detail}")
        self.region = region
        self.detail = detail


class UnavailableError(AppError):
    """No quorum reachable (minority partition), so a new room cannot be placed
    here. Already-committed rooms still serve."""

    status_code = 503
    message = "unavailable"


class MediaError(AppError):
    """Base for anything on the backbone that costs one datagram and nothing more.

    The backbone pump catches *this*, once. Never client-safe — these do not reach
    an HTTP caller, and the detail is a map of how far into the relay framing a
    prober got.
    """

    status_code = 400
    message = "bad datagram"
    client_safe = False


class TruncatedError(MediaError):
    """A relay datagram shorter than its framing requires.

    In Python this failure is quiet, which makes it worse: `buf[4:8]` on a
    three-byte buffer does not raise, it returns three bytes, and the bug surfaces
    later as a nonsensical stream id. Check the length first, then slice.
    """

    message = "truncated datagram"

    def __init__(self, *, need: int, got: int) -> None:
        super().__init__(f"truncated: need at least {need} bytes, got {got}")
        self.need = need
        self.got = got


class UnknownPeerError(MediaError):
    """A relay datagram from an address that is not a known peer's backbone
    endpoint. Dropped — accepting it would let a stranger inject media onto the
    backbone."""

    message = "relay datagram from an unknown peer"


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc
    if exc.status_code >= 500 or not exc.client_safe:
        logger.warning("request failed", error=str(exc), kind=type(exc).__name__)
    return JSONResponse(status_code=exc.status_code, content=exc.body())


async def _not_implemented(_request: Request, exc: Exception) -> JSONResponse:
    """Render an unbuilt vertical as `501 Not Implemented`, naming the todo.

    Without this, a handler that reaches `raise NotImplementedError` is a bare 500
    and a traceback. With it, `curl` tells you which vertical you are on — the
    first `publish` answers with V1's `place_room` message.
    """
    logger.warning("reached an unbuilt path", todo=str(exc))
    return JSONResponse(status_code=501, content={"error": "not implemented", "todo": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    """Register the `AppError` -> HTTP mapping and the scaffold's 501."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(NotImplementedError, _not_implemented)
