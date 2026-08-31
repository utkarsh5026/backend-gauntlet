"""A single application error family that maps itself to an HTTP response.

This is the Python shape of what `error.rs` did with an enum: a small hierarchy
with the status code as a class attribute. Raising beats returning a result type
here — the proxy path is route match -> backend pick -> forward, and any of the
three can fail four frames deep from the handler. `raise NoRoute()` needs no
plumbing in the frames between.

The variants are the gateway's vocabulary, and getting them *distinct* is graded:
"nothing matched this request" (404), "something matched but its whole pool is
down" (503), "the backend refused the connection" (502) and "the backend accepted
and then said nothing" (504) are four different operational problems. Collapsing
them into one 502 is the difference between an on-call page you can act on and
one you have to reproduce first.

## The 5xx rule, with a proxy-shaped exception

The usual rule — log the detail, return something generic — exists so internals
never leak to a client. But three of this service's 5xx codes are not internal
failures at all: 502, 503 and 504 are the gateway's honest, *deliberate* report
about the upstream, and a client (or a retry layer above it) is entitled to tell
them apart. So only a genuine, unexpected 500 gets the generic treatment; the
gateway's own status codes keep their message and say nothing about the topology
beyond what the status code already implies. What must never appear in any of
them is the backend's address — that is the internal detail worth protecting, and
it belongs in the log line, not the body.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "BadGateway",
    "GatewayTimeout",
    "InvalidRequest",
    "NoHealthyBackend",
    "NoRoute",
    "PayloadTooLarge",
    "app_error_handler",
    "install_error_handlers",
]

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this gateway turns into a response."""

    status_code: int = 500
    message: str = "internal server error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class NoRoute(AppError):
    """No route in the table matched this request (V2).

    Distinct from `NoHealthyBackend` on purpose, and V2 grades the distinction:
    404 means *you asked for something this gateway does not front*, which is a
    client problem and a config question. 503 means *we know where that goes and
    it is down*, which is an operations problem. Same request, entirely different
    person gets woken up.
    """

    status_code = 404
    message = "no route matches this request"


class NoHealthyBackend(AppError):
    """A route matched, but every backend in its pool is unhealthy or
    open-circuit (V3/V4).

    Worth returning *fast*. This is the fail-fast payoff V4 is measured on: the
    gateway already knows the pool is down, so making the client wait the upstream
    deadline to be told so just spends the timeout twice.
    """

    status_code = 503
    message = "no healthy backend available"


class BadGateway(AppError):
    """The upstream connection or transport failed — refused, reset, DNS (V1)."""

    status_code = 502
    message = "bad gateway"


class GatewayTimeout(AppError):
    """The upstream did not respond within the request deadline (V1).

    The distinction from `BadGateway` is the one worth being pedantic about: a
    refusal is immediate and unambiguous, a timeout means the request may well
    have been *received and acted on* upstream. That is why only idempotent
    requests may be retried after this one — see the retry-budget item in the
    SPEC's horizontal checklist.
    """

    status_code = 504
    message = "upstream timed out"


class PayloadTooLarge(AppError):
    """The request body exceeded `MAX_BODY_BYTES` (security horizontal)."""

    status_code = 413
    message = "request body too large"


class InvalidRequest(AppError):
    """The request was malformed — bad header, bad target."""

    status_code = 400
    message = "invalid request"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to its response.

    Typed against `Exception` rather than `AppError` because that is the signature
    Starlette's handler registry expects; the narrowing happens here.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if exc.status_code >= 500:
        # Full detail to the log either way — the log is where the backend
        # address and the underlying transport error belong.
        logger.error("request failed", error=str(exc), kind=type(exc).__name__)

    # Only an unexpected 500 is anonymized; see the module docstring on why the
    # gateway's own 502/503/504 keep their meaning.
    body = {"error": AppError.message if exc.status_code == 500 else exc.message}
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the AppError -> HTTP mapping on the app."""
    app.add_exception_handler(AppError, app_error_handler)
