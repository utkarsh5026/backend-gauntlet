"""The AWS error envelope — the half of an API that only matters during incidents.

Every Tier 8 service was growing its own copy of this: a four-field `AppError`
base, a taxonomy, and a handler that renders it. The taxonomy is genuinely
per-service (`QueueDoesNotExist` means nothing to DynamoDB) but the *envelope* is
not, and neither is the policy around it. That split is the whole design here:
this module owns the base class, the errors every AWS service returns, and the
rendering; a service owns its own named errors, as subclasses.

Three things are load-bearing, and all three are behaviour an SDK depends on:

* **`retryable` is a wire contract, not a comment.** A client decides whether to
  back off by reading the error code. A throttle means *come back shortly*; a
  failed condition means *your assumption was wrong and retrying unchanged will
  fail again*. Getting this backwards produces either an infinite retry loop or a
  client that gives up on a transient blip.

* **Client errors are 400, not 404.** The AWS JSON protocol answers 400 for
  almost everything a caller got wrong, a missing resource included. It offends
  HTTP sensibilities and it is right: the transport succeeded, the *request* was
  bad. Services that would rather use HTTP-meaningful codes can — the status is a
  class attribute — but pick one convention and write it in the design doc.

* **A 5xx never carries its instance message.** The detail goes to the log; the
  caller gets the class default, which we authored and know is safe. An
  exception message on a 500 has, at various points in this repo, been able to
  contain a queue name, a receipt handle, or a row of somebody's data.

Deliberately **not** here: anything that verifies a signature. The auth error
codes below are what a service *answers with*; computing the answer is project
25's V1, and a shared SigV4 implementation would hand that vertical away.
"""

from __future__ import annotations

import enum
import xml.etree.ElementTree as ET
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .wire import REQUEST_ID_HEADER, WireProtocol, new_request_id

__all__ = [
    "AccessDenied",
    "AwsError",
    "ExpiredToken",
    "Fault",
    "IncompleteSignature",
    "InternalFailure",
    "InvalidAction",
    "InvalidClientTokenId",
    "InvalidParameterValue",
    "MissingAction",
    "MissingAuthenticationToken",
    "MissingParameter",
    "ResourceNotFound",
    "SerializationException",
    "ServiceUnavailable",
    "SignatureDoesNotMatch",
    "ThrottlingException",
    "ValidationException",
    "current_request_id",
    "error_body",
    "error_response",
    "install_error_handlers",
]

log = structlog.get_logger(__name__)

# The Query protocol wraps errors in a namespaced document. botocore ignores the
# namespace when parsing, but a namespace-aware client will not, so emit it.
QUERY_ERROR_NAMESPACE = "https://queue.amazonaws.com/doc/2012-11-05/"


class Fault(enum.Enum):
    """Whose fault the error is — the one bit of an error a client can act on.

    Two spellings, because AWS uses two. The Query protocol says `Sender` /
    `Receiver`; Lambda's REST-JSON surface says `User` / `Service`. Same idea,
    a decade apart.
    """

    CLIENT = "client"
    SERVER = "server"

    @property
    def query_name(self) -> str:
        return "Sender" if self is Fault.CLIENT else "Receiver"

    @property
    def rest_name(self) -> str:
        return "User" if self is Fault.CLIENT else "Service"


class AwsError(Exception):
    """Base for every error a service turns into a response.

    Field names match what projects 23/24/25/29 already wrote by hand, so
    adopting this package is deleting a base class and an import, not a rewrite:
    subclasses keep declaring `status_code`, `error_code`, `message` and
    `retryable` exactly as they do today.
    """

    status_code: int = 500
    error_code: str = "InternalFailure"
    message: str = "an internal error occurred"
    retryable: bool = False

    #: How long a client should wait before retrying, when `retryable`. A number
    #: rather than a lookup table on the client side — the service knows.
    retry_after_seconds: int = 1

    #: Set only when the derived value below is wrong for this error. AWS marks
    #: throttles as the *sender's* fault even though nothing about the sender was
    #: malformed, so the derivation is right far more often than it looks.
    fault: Fault | None = None

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message

    @property
    def sender_fault(self) -> bool:
        """True when the caller caused it — also the `SenderFault` batch field."""
        if self.fault is not None:
            return self.fault is Fault.CLIENT
        return self.status_code < 500

    @property
    def resolved_fault(self) -> Fault:
        return Fault.CLIENT if self.sender_fault else Fault.SERVER

    @property
    def safe_message(self) -> str:
        """What a caller may see: the instance message, unless this is a 5xx."""
        if self.status_code >= 500:
            return type(self).message
        return self.message


# --- the errors every AWS service returns -----------------------------------
#
# A service subclasses these or defines its own alongside them. Only the ones
# that are genuinely universal live here: the moment a code means something
# service-specific (`QueueDoesNotExist`, `ConditionalCheckFailedException`) it
# belongs to that service, where the docstring can say what it means *there*.


class InternalFailure(AwsError):
    """The unhandled case. Retryable because it might have been a blip."""

    retryable = True


class ServiceUnavailable(AwsError):
    """Deliberately shedding load, or a dependency is down."""

    status_code = 503
    error_code = "ServiceUnavailable"
    message = "the service is temporarily unavailable"
    retryable = True


class ValidationException(AwsError):
    """The request is structurally wrong: missing field, illegal combination."""

    status_code = 400
    error_code = "ValidationException"
    message = "the request is not valid"


class SerializationException(AwsError):
    """The body could not be parsed at all — not JSON, not the expected shape."""

    status_code = 400
    error_code = "SerializationException"
    message = "the request body could not be deserialized"


class MissingParameter(AwsError):
    status_code = 400
    error_code = "MissingParameter"
    message = "a required parameter is missing"


class InvalidParameterValue(AwsError):
    """Present, parseable, and out of bounds. The catch-all for limits."""

    status_code = 400
    error_code = "InvalidParameterValue"
    message = "invalid value for a parameter"


class MissingAction(AwsError):
    """No `X-Amz-Target` header (JSON) or no `Action` parameter (Query)."""

    status_code = 400
    error_code = "MissingAction"
    message = "the request is missing an action"


class InvalidAction(AwsError):
    """An action this service does not answer."""

    status_code = 400
    error_code = "InvalidAction"
    message = "the action is not valid for this service"


class ResourceNotFound(AwsError):
    """No such table, function, queue or role.

    400 by default, following the JSON protocol. A service that would rather say
    404 overrides `status_code`; one whose code differs (`NoSuchEntity`,
    `QueueDoesNotExist`) overrides `error_code`.
    """

    status_code = 400
    error_code = "ResourceNotFoundException"
    message = "the requested resource was not found"


class ThrottlingException(AwsError):
    """Rate limited. The one error clients are *expected* to retry."""

    status_code = 429
    error_code = "ThrottlingException"
    message = "rate exceeded"
    retryable = True


# --- auth: the codes a service answers with, never the verification ---------
#
# These exist here because every service in the tier has to be able to say them,
# and because the exact code decides what an SDK does next: `ExpiredToken` makes
# botocore refresh credentials and retry, `SignatureDoesNotMatch` makes it stop.
# The *deciding* lives in project 25.


class MissingAuthenticationToken(AwsError):
    status_code = 403
    error_code = "MissingAuthenticationToken"
    message = "the request is missing authentication information"


class IncompleteSignature(AwsError):
    status_code = 400
    error_code = "IncompleteSignature"
    message = "the authorization header is malformed"


class InvalidClientTokenId(AwsError):
    status_code = 403
    error_code = "InvalidClientTokenId"
    message = "the security token included in the request is invalid"


class SignatureDoesNotMatch(AwsError):
    status_code = 403
    error_code = "SignatureDoesNotMatch"
    message = "the request signature does not match the signature we computed"


class ExpiredToken(AwsError):
    """Credentials expired. Retryable *after* the client refreshes them."""

    status_code = 403
    error_code = "ExpiredTokenException"
    message = "the security token included in the request is expired"
    retryable = True


class AccessDenied(AwsError):
    """Authenticated, and not permitted.

    Never conflate this with a 5xx. A deny is a decision the service made and
    stands behind; a 500 is the service admitting it does not know. Ranking them
    together in a dashboard is how a broken authorizer hides for a week.
    """

    status_code = 403
    error_code = "AccessDenied"
    message = "access denied"


# --- rendering --------------------------------------------------------------


def error_body(exc: AwsError, *, protocol: WireProtocol, request_id: str) -> str | dict[str, Any]:
    """The response body for an error, in this protocol's shape.

    Returns a dict for the JSON protocols and an XML string for Query, because
    that is the difference between them — `JSONResponse` serializes the first,
    the second is already serialized.
    """
    message = exc.safe_message

    if protocol is WireProtocol.QUERY:
        root = ET.Element("ErrorResponse", {"xmlns": QUERY_ERROR_NAMESPACE})
        error = ET.SubElement(root, "Error")
        ET.SubElement(error, "Type").text = exc.resolved_fault.query_name
        ET.SubElement(error, "Code").text = exc.error_code
        ET.SubElement(error, "Message").text = message
        ET.SubElement(root, "RequestId").text = request_id
        return ET.tostring(root, encoding="unicode")

    if protocol is WireProtocol.REST_JSON:
        # Lambda's shape: the fault is a field, and there is no `__type`.
        return {"Type": exc.resolved_fault.rest_name, "message": message}

    # AWS JSON 1.0 / 1.1. `__type` is what botocore reads to name the error; it
    # accepts both a bare code and a `shape#Code` form, and bare is kinder to
    # read in a terminal.
    return {"__type": exc.error_code, "message": message}


def error_response(
    exc: AwsError,
    *,
    protocol: WireProtocol = WireProtocol.JSON_1_0,
    request_id: str | None = None,
) -> Response:
    """Render an `AwsError` as the response an AWS SDK expects."""
    rid = request_id or new_request_id()
    headers = {
        # Set on both paths: botocore reads the header when the body is
        # unparseable, which is exactly when you most want to know the code.
        "x-amzn-errortype": exc.error_code,
        REQUEST_ID_HEADER: rid,
    }
    if exc.retryable:
        headers["retry-after"] = str(exc.retry_after_seconds)

    body = error_body(exc, protocol=protocol, request_id=rid)
    if isinstance(body, str):
        return Response(
            body, status_code=exc.status_code, headers=headers, media_type=protocol.content_type
        )
    return JSONResponse(
        body, status_code=exc.status_code, headers=headers, media_type=protocol.content_type
    )


def install_error_handlers(
    app: Starlette, *, protocol: WireProtocol = WireProtocol.JSON_1_0
) -> None:
    """Register the two handlers every service in the tier needs.

    The second one — the catch-all — is the one worth arguing about. It exists so
    that an unexpected exception becomes a loud 500 rather than anything else. On
    a queue the dangerous accident is not a 500, it is a 200: a `SendMessage`
    that reports success after failing to enqueue has silently eaten a customer's
    message and no retry will ever happen. On an authorizer the dangerous
    accident is an allow. Both are the same rule — fail closed.

    Note that FastAPI/Starlette only routes to the catch-all when
    `raise_server_exceptions` is off, i.e. in production behind uvicorn. Under
    the test transport the exception propagates instead, which is what lets a
    scaffold's tests assert on `NotImplementedError` directly.
    """

    async def handle_aws_error(_request: Request, exc: Exception) -> Response:
        if not isinstance(exc, AwsError):  # pragma: no cover - registry invariant
            raise exc
        if exc.status_code >= 500:
            log.error("request failed", error=str(exc), kind=type(exc).__name__)
        return error_response(exc, protocol=protocol, request_id=current_request_id())

    async def handle_unexpected(_request: Request, exc: Exception) -> Response:
        log.error("unhandled exception", error=str(exc), kind=type(exc).__name__)
        return error_response(InternalFailure(), protocol=protocol, request_id=current_request_id())

    app.add_exception_handler(AwsError, handle_aws_error)
    app.add_exception_handler(Exception, handle_unexpected)


def current_request_id() -> str:
    """The id bound to this request by `common-telemetry`, or a fresh one.

    Read straight out of structlog's contextvars rather than by importing
    `common-telemetry`, which keeps the two packages independent while making
    them agree: the id in `x-amzn-requestid` is the same id on every log line
    emitted while serving the request. That is the whole point of returning one —
    a caller quoting an id in a bug report should land you in the right logs.
    """
    bound: dict[str, Any] = structlog.contextvars.get_contextvars()
    value = bound.get("request_id")
    return value if isinstance(value, str) else new_request_id()
