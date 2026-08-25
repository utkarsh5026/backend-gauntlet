"""AWS's named errors, mapped to HTTP responses.

Handlers raise these; one handler does the mapping, so status-code policy lives
in exactly one place. The names match the real service, because a
`SignatureDoesNotMatch` you debug here is the same one you will debug at work.

Three things here are load-bearing, and the SPEC grades all three:

* **Fail closed.** The catch-all handler at the bottom exists because the
  dangerous failure in an authorization service is not a 500 — it is an
  *allow* returned by accident. Anything unexpected must become a denial, and
  the one place that is guaranteed to run on every unhandled exception is here.

* **Say nothing you were not asked.** A 5xx message may contain an internal
  detail; a 403 message may reveal whether a principal exists. Server errors are
  logged in full and answered with the class's own default text, which we wrote
  and know is safe.

* **Retryable or not.** `Throttling` means *come back shortly*. `AccessDenied`
  means *nothing about retrying will help*. A client that retries the second one
  is a client in an infinite loop, and the header is what tells it apart.

> **Wire format.** The real IAM/STS Query protocol answers XML
> (`<ErrorResponse><Error><Code>…`). This scaffold answers the AWS **JSON**
> protocol's shape (`__type` plus `x-amzn-errortype`) because it is
> straightforward to assert against. Making the Query surface XML-faithful enough
> for `boto3` is a horizontal checklist item, not a freebie.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AccessDenied",
    "AppError",
    "EntityAlreadyExists",
    "ExpiredToken",
    "IncompleteSignature",
    "InvalidAction",
    "InvalidClientTokenId",
    "LimitExceeded",
    "MalformedPolicyDocument",
    "MissingAction",
    "MissingAuthenticationToken",
    "NoSuchEntity",
    "SignatureDoesNotMatch",
    "Throttling",
    "app_error_handler",
    "install_error_handlers",
    "unhandled_error_handler",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    error_code: str = "ServiceFailure"
    message: str = "internal service error"
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


# --- authentication (V1/V4) -------------------------------------------------
# Every one of these is a 403 in the real service, and telling them apart is the
# whole difference between a five-minute fix and an afternoon. The SPEC's
# observability section requires them to be counted separately for exactly that
# reason: a spike in skew is an NTP problem, a spike in mismatch is a client
# bug, and a spike in unknown-key is someone probing you.


class MissingAuthenticationToken(AppError):
    """No `Authorization` header and no presigned query parameters.

    Not the same as a bad signature: the caller did not even try. Rejected before
    anything else runs, so an unauthenticated caller learns nothing about what
    exists here.
    """

    status_code = 403
    error_code = "MissingAuthenticationToken"
    message = "request is missing authentication information"


class IncompleteSignature(AppError):
    """The `Authorization` header is present but malformed — a parse failure."""

    status_code = 400
    error_code = "IncompleteSignature"
    message = "the authorization header is malformed"


class InvalidClientTokenId(AppError):
    """The access key id is unknown, inactive, or revoked.

    Deliberately worded identically for all three: distinguishing "no such key"
    from "deactivated key" in the *response* hands an attacker a key-id oracle.
    The distinction belongs in the audit trail, not on the wire.
    """

    status_code = 403
    error_code = "InvalidClientTokenId"
    message = "the security token included in the request is invalid"


class SignatureDoesNotMatch(AppError):
    """The computed signature differs from the presented one — or the clock does.

    Real AWS returns this both for a genuine mismatch and for excessive clock
    skew, and the message is where it tells you which. V1 requires skew to be
    *distinguishable*, so raise it with an explicit message when that is the
    cause.
    """

    status_code = 403
    error_code = "SignatureDoesNotMatch"
    message = "the request signature does not match the signature you provided"


class ExpiredToken(AppError):
    """The temporary credentials are past their expiry (V4)."""

    status_code = 403
    error_code = "ExpiredTokenException"
    message = "the security token included in the request is expired"


# --- authorization (V3) -----------------------------------------------------


class AccessDenied(AppError):
    """The evaluation chain said no.

    Not retryable, and it must never be conflated with a 5xx: a deny is this
    service working correctly. The SPEC requires denies to be counted apart from
    errors for the same reason project 24 separates throttles from failures — a
    correct refusal in your error rate is how a policy problem gets misdiagnosed
    as an outage.

    `reason` carries the *why* (implicit vs explicit deny, and which layer) for
    the audit trail and the metric label. Whether it also goes on the wire is a
    real decision: the real service does tell you, which is enormously helpful
    for debugging and mildly helpful to an attacker mapping your permissions.
    Make the call deliberately and write it in `docs/25-design.md`.
    """

    status_code = 403
    error_code = "AccessDenied"
    message = "access denied"

    def __init__(self, message: str | None = None, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# --- management plane -------------------------------------------------------


class MissingAction(AppError):
    """No `Action` parameter — the Query protocol's "what did you want?"."""

    status_code = 400
    error_code = "MissingAction"
    message = "no action was supplied with this request"


class InvalidAction(AppError):
    """An `Action` this service does not implement."""

    status_code = 400
    error_code = "InvalidAction"
    message = "the action requested is not valid for this service"


class NoSuchEntity(AppError):
    """No such user, role, policy, or session."""

    status_code = 404
    error_code = "NoSuchEntity"
    message = "the requested entity does not exist"


class EntityAlreadyExists(AppError):
    """A user, role or policy with that name already exists."""

    status_code = 409
    error_code = "EntityAlreadyExists"
    message = "an entity with that name already exists"


class MalformedPolicyDocument(AppError):
    """The policy document is unparseable, oversized, or over-nested (V2).

    Raised at **write** time on purpose. A policy that only fails at evaluation
    time is a policy that fails on the hot path, where the only safe answer left
    is to deny — which looks to everyone involved like an outage.
    """

    status_code = 400
    error_code = "MalformedPolicyDocument"
    message = "the policy document is not valid"


class LimitExceeded(AppError):
    """A documented limit was hit: policy size, count, chain depth, condition keys."""

    status_code = 409
    error_code = "LimitExceeded"
    message = "the request exceeds a service limit"


class Throttling(AppError):
    """Rate limited.

    The real Query protocol answers 400 for this; 429 is used here because it is
    the honest status code and every HTTP client already understands it. Mirroring
    the Query protocol exactly — status code included — is a horizontal checklist
    item; if you take it, this is one of the lines that changes.
    """

    status_code = 429
    error_code = "Throttling"
    message = "rate exceeded"
    retryable = True


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to an AWS-shaped error body."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    message = exc.message
    if exc.status_code >= 500:
        # Never hand an instance message to a caller on this path — it may carry
        # an internal detail. The detail goes to the log; the caller gets the
        # class default, which we authored and know is safe.
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        message = type(exc).message

    headers = {"x-amzn-errortype": exc.error_code}
    if exc.retryable:
        headers["retry-after"] = "1"
    return JSONResponse(
        status_code=exc.status_code,
        content={"__type": exc.error_code, "message": message},
        headers=headers,
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Fail closed: anything unexpected becomes a refusal, never an allow.

    This is the outermost expression of the SPEC's "the service fails closed
    everywhere" criterion. An authorization service that answers 500 has at least
    said *no*; one that answers 200 because an exception unwound through an
    optimistic `except` has said *yes* to something nobody approved.

    It deliberately does not swallow the exception: the full detail is logged, and
    the caller gets a generic refusal with nothing in it.
    """
    log.error("unhandled exception — denying", error=str(exc), kind=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"__type": AppError.error_code, "message": AppError.message},
        headers={"x-amzn-errortype": AppError.error_code},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    # NOTE: FastAPI only routes to this when `raise_server_exceptions` is off,
    # i.e. in production behind uvicorn — under the test transport the exception
    # propagates instead, which is what lets the scaffold tests assert on
    # `NotImplementedError` directly.
    app.add_exception_handler(Exception, unhandled_error_handler)
