"""Lambda's named exceptions, mapped to HTTP responses.

Handlers raise these; one handler does the mapping, so status-code policy lives in
exactly one place. The names match the real service so the mental model transfers —
if you have ever seen a `TooManyRequestsException` in a retry log, this is the
thing that was throwing it.

Two distinctions here are load-bearing, and both are graded by the SPEC:

* `retryable` — a throttle means *come back shortly*; a bad payload means *fix it
  first*. A client that retries the second one is a client in an infinite loop.
* **function error vs. platform error** — the handler raising is not the platform
  failing. Real Lambda signals this with an `X-Amz-Function-Error` header on an
  otherwise-200 response, because from the platform's point of view the invocation
  succeeded: it ran your code and your code threw. Conflating the two is how a
  dashboard ends up blaming the wrong team.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AppError",
    "EnvironmentFailure",
    "FunctionError",
    "InvalidRequestContent",
    "InvocationTimedOut",
    "RequestTooLarge",
    "ResourceConflict",
    "ResourceNotFound",
    "TooManyRequests",
    "app_error_handler",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)

# The header the real service sets when the *handler* failed rather than the
# platform. Callers branch on its presence, so it is part of the contract.
FUNCTION_ERROR_HEADER = "x-amz-function-error"


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    error_code: str = "ServiceException"
    message: str = "internal service error"
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class InvalidRequestContent(AppError):
    """Malformed request: unparseable payload, unknown invocation type, bad config."""

    status_code = 400
    error_code = "InvalidRequestContentException"
    message = "invalid request content"


class RequestTooLarge(AppError):
    """The payload exceeds the cap for this invocation path (sync and async differ)."""

    status_code = 413
    error_code = "RequestTooLargeException"
    message = "request payload exceeds the maximum allowed size"


class ResourceNotFound(AppError):
    """No such function, alias, or event source mapping."""

    status_code = 404
    error_code = "ResourceNotFoundException"
    message = "requested resource not found"


class ResourceConflict(AppError):
    """The resource already exists, or is in a state that forbids this operation."""

    status_code = 409
    error_code = "ResourceConflictException"
    message = "resource already exists or is in a conflicting state"


class TooManyRequests(AppError):
    """Concurrency exhausted (V4) — the one callers are expected to back off and retry.

    Note this is a *capacity* signal, not a failure: the SPEC requires it to be
    counted separately from errors, because a throttle in your error rate is how a
    capacity problem gets misdiagnosed as a bug.
    """

    status_code = 429
    error_code = "TooManyRequestsException"
    message = "rate exceeded: no concurrency available for this function"
    retryable = True


class InvocationTimedOut(AppError):
    """The handler did not respond before its deadline (V1/V3).

    Not retryable by the platform on the synchronous path — the caller decides,
    because only the caller knows whether the operation was idempotent.
    """

    status_code = 504
    error_code = "InvocationTimedOutException"
    message = "the invocation timed out before the handler responded"


class EnvironmentFailure(AppError):
    """The platform could not create or keep an execution environment (V2/V3).

    Deliberately distinct from `FunctionError`: init blew up, the sandbox was OOM
    killed, the process died. This one is the platform's fault, and the SPEC asks
    for it to be reported as such.
    """

    status_code = 500
    error_code = "EnvironmentFailureException"
    message = "the execution environment failed"
    retryable = True


class FunctionError(AppError):
    """The handler itself raised.

    From the platform's side this is a **successful** invocation, which is why the
    real API answers 200 with `X-Amz-Function-Error` set rather than a 5xx. The
    payload carries the handler's own error type and stack trace.
    """

    status_code = 200
    error_code = "Unhandled"
    message = "the function raised an error"

    def __init__(
        self,
        message: str | None = None,
        *,
        error_type: str = "Unhandled",
        stack_trace: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.stack_trace = stack_trace or []


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to a Lambda-shaped error body."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    if isinstance(exc, FunctionError):
        # A handler error is not a platform error: 200 + the marker header, with
        # the function's own diagnostics in the body.
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "errorType": exc.error_type,
                "errorMessage": exc.message,
                "stackTrace": exc.stack_trace,
            },
            headers={FUNCTION_ERROR_HEADER: "Unhandled"},
        )

    message = exc.message
    if exc.status_code >= 500:
        # Never leak an instance message to a caller — on this path it may carry a
        # sandbox's stderr. The detail goes to the log; the caller gets the class's
        # own default, which we authored and know is safe.
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        message = type(exc).message

    body = {"Type": "User" if exc.status_code < 500 else "Service", "message": message}
    headers = {"x-amzn-errortype": exc.error_code}
    if exc.retryable:
        # Tell the client it may retry, so a correct backoff needs no lookup table.
        headers["retry-after"] = "1"
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
