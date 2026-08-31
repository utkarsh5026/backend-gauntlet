"""A single application error family that maps itself to an HTTP response.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here — `raise NotLeader(...)` from four frames deep needs no plumbing in between,
which matters in a codebase where the "am I still the leader?" check happens deep
inside the replication path.

The variant that makes this project different is `NotLeader`: in Raft, writes
(and linearizable reads) may only be served by the leader. A follower that
receives one doesn't guess — it redirects the client to the leader it currently
believes in. That's a protocol decision, so it lives here, and it is the one
error that carries data rather than just a message.

Full detail is logged only on 5xx, so internals (paths, transport messages) never
reach a client.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .rpc import NodeId

__all__ = [
    "AppError",
    "InvalidRequest",
    "KeyNotFound",
    "NotLeader",
    "StorageError",
    "TransportError",
    "UnknownPeer",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotLeader(AppError):
    """This node is not the leader.

    Carries the leader's client address when known, so the handler can redirect
    instead of merely failing. The status is a **503** when the leader is unknown
    (the cluster is mid-election — the honest answer is "retry shortly") and a
    **307** when it is known, because a redirect a client can follow is strictly
    more useful than an error it has to interpret.
    """

    status_code = 503
    message = "not the leader"

    def __init__(
        self,
        leader_id: NodeId | None = None,
        leader_addr: str | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.leader_id = leader_id
        self.leader_addr = leader_addr


class KeyNotFound(AppError):
    """A read hit a key that isn't in the state machine."""

    status_code = 404
    message = "key not found"


class UnknownPeer(AppError):
    """The request named a peer that isn't in this node's cluster config."""

    status_code = 404
    message = "unknown peer"


class TransportError(AppError):
    """A peer RPC failed at the transport layer (connection refused, timeout, ...).

    Expected and normal in a cluster — a node being unreachable is the failure
    Raft exists to tolerate, not a bug. It is an error *type* only so that the
    consensus code has something to catch; catching it and carrying on is the
    correct handling almost everywhere it can be raised.
    """

    status_code = 502
    message = "peer transport error"


class InvalidRequest(AppError):
    """The request was malformed (bad key, bad node id, ...)."""

    status_code = 400
    message = "invalid request"


class StorageError(AppError):
    """A filesystem operation on the persistent state failed.

    A 500 on purpose, and a serious one: if the persistent state cannot be
    written, this node must not answer the RPC that depended on it. Raft's safety
    argument assumes a durable vote and a durable log.
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

    # A known leader gets a real redirect so a client (or a dumb load balancer)
    # can follow it to the node that can actually serve the write.
    if isinstance(exc, NotLeader) and exc.leader_addr is not None:
        return JSONResponse(
            status_code=307,
            headers={"location": f"http://{exc.leader_addr}"},
            content={"error": exc.message, "leader": exc.leader_addr},
        )

    if exc.status_code >= 500:
        # Full detail to the log, a generic string to the caller.
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"error": AppError.message}
    else:
        body = {"error": exc.message}
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
