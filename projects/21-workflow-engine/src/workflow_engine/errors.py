"""One application error family that knows its own gRPC status code.

Engine methods raise; the servicer catches at the edge and turns the exception
into a status. Python's exception hierarchy is the natural shape here: raising
from four frames down needs no plumbing in between, which is the ergonomics
`Result` + `?` buys in Rust.

The mapping matters more in gRPC than in a REST service, because clients *retry
on status code*, and here the client is a worker in a hot poll loop:

* `UNAVAILABLE` — "try again, maybe on another instance". Right for a store blip.
* `FAILED_PRECONDITION` — "your workflow code diverged from history; retrying
  the same task will fail the same way". This is the non-determinism signal, and
  the SPEC grades it: a worker author must be able to tell "my bug" from "the
  engine broke" by status code alone.
* `INVALID_ARGUMENT` — "you sent something we can't act on". Never retryable.
* `INTERNAL` — "don't bother, this is broken".

The other half of the policy: log the whole error server-side, hand the client a
generic string for anything internal. A durable-execution engine's internals
include DSNs and other executions' ids, and workers are not all friendly.
"""

from __future__ import annotations

from typing import NoReturn

import grpc
import grpc.aio
import structlog

__all__ = [
    "AppError",
    "CorruptHistory",
    "Internal",
    "InvalidArgument",
    "NonDeterministic",
    "NotFound",
    "Store",
    "abort",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this engine turns into a gRPC status."""

    status_code: grpc.StatusCode = grpc.StatusCode.INTERNAL
    message: str = "internal error"
    #: Whether the caller may see `str(self)` rather than the generic `message`.
    public: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NotFound(AppError):
    """No such execution / run id."""

    status_code = grpc.StatusCode.NOT_FOUND
    message = "workflow execution not found"
    public = True


class InvalidArgument(AppError):
    """The caller sent something we cannot act on.

    A bad run id, an empty task queue, a token that doesn't parse, an unknown
    command type, an oversize payload. Public: the caller can fix it, so saying
    what is wrong is help, not a leak.
    """

    status_code = grpc.StatusCode.INVALID_ARGUMENT
    message = "invalid argument"
    public = True


class NonDeterministic(AppError):
    """A worker's replay diverged from the recorded history (V2).

    The commands it returned contradict what the engine already knows happened —
    the workflow code is not a pure function of its history, usually because it
    was changed under a running execution.

    `FAILED_PRECONDITION`, deliberately: the request was well-formed, and
    retrying the same task will not help until the workflow code is fixed. This
    is the worker's bug, not a server fault, so it is emphatically not a 500 —
    and the message is a workflow author's best debugging clue, so it goes out
    verbatim ("expected X, got Y").
    """

    status_code = grpc.StatusCode.FAILED_PRECONDITION
    message = "nondeterministic workflow"
    public = True


class CorruptHistory(AppError):
    """A stored history could not be folded: a gap, an out-of-order id, an
    event referencing an activity or timer that was never scheduled.

    Not public and not the caller's fault — this is the engine's own durable
    truth failing its invariants, which is the one thing event sourcing exists to
    make impossible. It should page someone, not be papered over into a
    plausible-but-wrong state.
    """

    status_code = grpc.StatusCode.INTERNAL
    message = "internal error"


class Store(AppError):
    """The durable store (Postgres) failed.

    `UNAVAILABLE`, not `INTERNAL`: the engine's logic is fine and a retry may
    well land on a healthy path. A polling worker that gets `UNAVAILABLE` backs
    off and comes back, which is exactly the behaviour you want here.
    """

    status_code = grpc.StatusCode.UNAVAILABLE
    message = "workflow store unavailable"


class Internal(AppError):
    """Anything unexpected. Logged in full, reported as a generic string."""

    status_code = grpc.StatusCode.INTERNAL
    message = "internal error"


async def abort[Req, Resp](
    context: grpc.aio.ServicerContext[Req, Resp],
    exc: AppError,
) -> NoReturn:
    """End the RPC with `exc`'s status. Never returns.

    Generic over the request/response pair (PEP 695 syntax) because
    `ServicerContext` is *invariant* in both: a
    `ServicerContext[StartWorkflowRequest, StartWorkflowResponse]` is not a
    `ServicerContext[object, object]`, so widening would not type-check.

    `context.abort` raises to unwind the handler, which is why this is
    `NoReturn` — and saying so is not decoration. It is what lets the type
    checker prove that a value assigned inside `try:` is bound below the
    `except`, instead of warning that it might not be.
    """
    if exc.public:
        detail = exc.message
    else:
        # Full detail to the log, a fixed string to the caller.
        log.error("rpc failed", error=str(exc), kind=type(exc).__name__)
        detail = type(exc).message
    await context.abort(exc.status_code, detail)
