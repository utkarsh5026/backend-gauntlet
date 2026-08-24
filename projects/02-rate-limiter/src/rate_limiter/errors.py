"""One application error family that knows its own gRPC status code.

The Rust scaffold expressed this as an enum plus a `From<AppError> for Status`
impl. Python's equivalent is a small exception hierarchy with the status code as
a class attribute: raising from four frames deep needs no plumbing in between,
which is exactly the ergonomics `Result` + `?` buys in Rust.

The mapping matters more here than in a REST service, because gRPC clients
*retry on status code*. `UNAVAILABLE` tells a well-behaved client "try again,
possibly on another instance"; `INTERNAL` tells it "don't bother, this is
broken". Return the wrong one and you either lose availability or invite a retry
storm into an already-failing backend.

The other half of the policy: log the whole error server-side, hand the client a
generic string on anything 5xx-shaped. A rate limiter's internals include Redis
addresses and key names, and the callers are, by definition, sometimes hostile.
"""

from __future__ import annotations

from typing import NoReturn

import grpc
import grpc.aio
import structlog

__all__ = [
    "AppError",
    "Backend",
    "Internal",
    "InvalidArgument",
    "abort",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a gRPC status."""

    status_code: grpc.StatusCode = grpc.StatusCode.INTERNAL
    message: str = "internal error"
    #: Whether the client is safe to see `str(self)` rather than `message`.
    public: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class InvalidArgument(AppError):
    """The caller sent something we cannot act on — empty key, absurd cost.

    Public: the caller can fix this, so telling them how is not a leak. It is
    also non-retryable, and `INVALID_ARGUMENT` is how you say so.
    """

    status_code = grpc.StatusCode.INVALID_ARGUMENT
    message = "invalid argument"
    public = True


class Backend(AppError):
    """The Redis backend holding the shared state failed.

    `UNAVAILABLE`, not `INTERNAL`: the service itself is fine and a retry may
    well land on a healthy path. Note that reaching this at all means the
    fail-open/fail-closed policy did *not* apply — those two make a decision
    rather than an error, and this is what is left over.
    """

    status_code = grpc.StatusCode.UNAVAILABLE
    message = "rate limiter backend unavailable"


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
    `ServicerContext[CheckRequest, CheckResponse]` is not a
    `ServicerContext[object, object]`, so widening to `object` would not compile.

    `context.abort` raises to unwind the handler, which is why this is
    `NoReturn` — and saying so is not just decoration. It is what lets the type
    checker prove that the happy-path value below an `except` block is bound,
    instead of warning that it might not be.
    """
    if exc.public:
        detail = exc.message
    else:
        # Full detail to the log, a fixed string to the caller.
        log.error("rpc failed", error=str(exc), kind=type(exc).__name__)
        detail = type(exc).message
    await context.abort(exc.status_code, detail)
