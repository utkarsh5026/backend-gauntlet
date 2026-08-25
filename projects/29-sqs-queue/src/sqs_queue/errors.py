"""SQS's named errors, mapped to HTTP responses.

Handlers raise these; one handler does the mapping, so status-code policy lives
in exactly one place. The names match the real service, because a
`ReceiptHandleIsInvalid` you debug here is the same one you will debug at work —
and because the protocol horizontal's bar is that `boto3` recognises them. An SDK
decides whether to retry by reading `__type`, so a wrong code is not cosmetic: it
is the difference between a client backing off and a client giving up.

Three things here are load-bearing:

* **Client errors are 400, not 404.** The AWS JSON protocol answers 400 for
  nearly everything a caller got wrong, including a queue that does not exist.
  That looks wrong to HTTP sensibilities and is right for the protocol — the
  transport succeeded, the *request* was bad.

* **Retryable or not.** `RequestThrottled` and `OverLimit` mean *come back
  shortly*. `InvalidParameterValue` means *nothing about retrying will help*. A
  client that retries the second one is a client in an infinite loop.

* **A stale receipt handle is not a malformed one.** `ReceiptHandleIsInvalid`
  covers a handle that cannot be parsed or was never issued; a handle that was
  valid and has been superseded is an *honest slow worker*, and V1 asks you to
  decide what it deserves. The observability checklist counts the two separately
  because they mean completely different things: one is a client bug, the other
  is your visibility timeout being too short.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = [
    "AccessDenied",
    "AppError",
    "BatchEntryIdsNotDistinct",
    "EmptyBatchRequest",
    "InvalidAttributeName",
    "InvalidAttributeValue",
    "InvalidBatchEntryId",
    "InvalidMessageContents",
    "InvalidParameterValue",
    "MessageNotInflight",
    "MissingParameter",
    "OverLimit",
    "QueueDeletedRecently",
    "QueueDoesNotExist",
    "QueueNameExists",
    "ReceiptHandleIsInvalid",
    "RequestThrottled",
    "UnsupportedOperation",
    "app_error_handler",
    "install_error_handlers",
    "unhandled_error_handler",
]

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every error this service turns into a response."""

    status_code: int = 500
    error_code: str = "InternalError"
    message: str = "internal service error"
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


# --- the control plane (V6) -------------------------------------------------


class QueueDoesNotExist(AppError):
    """No queue by that name or URL.

    400, not 404 — the AWS JSON protocol's convention. Also raised for a queue
    that was deleted, so a stale URL cannot silently start working again if
    somebody recreates the name.
    """

    status_code = 400
    error_code = "QueueDoesNotExist"
    message = "the specified queue does not exist"


class QueueNameExists(AppError):
    """`CreateQueue` for an existing name with **different** attributes.

    The same name with identical attributes is not an error — it is the
    idempotent case, and it succeeds. This is only the genuine conflict, and V6
    grades getting the line between them right.
    """

    status_code = 400
    error_code = "QueueNameExists"
    message = "a queue with this name already exists with different attributes"


class QueueDeletedRecently(AppError):
    """The name was deleted less than 60 seconds ago.

    Real SQS refuses to recreate a name for a minute. It is not arbitrary: an
    in-flight request holding the old queue's URL must not land in a new queue
    that happens to share its name, and the delay is what guarantees the old
    URL is provably dead first.
    """

    status_code = 400
    error_code = "QueueDeletedRecently"
    message = (
        "you must wait 60 seconds after deleting a queue before creating one with the same name"
    )


class InvalidAttributeName(AppError):
    """An attribute this service does not know."""

    status_code = 400
    error_code = "InvalidAttributeName"
    message = "unknown attribute name"


class InvalidAttributeValue(AppError):
    """A known attribute with a value outside its documented bounds."""

    status_code = 400
    error_code = "InvalidAttributeValue"
    message = "invalid value for the attribute"


class UnsupportedOperation(AppError):
    """A FIFO-only operation on a standard queue, or the reverse."""

    status_code = 400
    error_code = "UnsupportedOperation"
    message = "the operation is not supported for this queue type"


# --- the data plane (V1, V3, V4, V5) ----------------------------------------


class MissingParameter(AppError):
    """A required parameter was not supplied."""

    status_code = 400
    error_code = "MissingParameter"
    message = "a required parameter is missing"


class InvalidParameterValue(AppError):
    """A parameter is present, parseable, and out of bounds.

    The catch-all for the caps in the security checklist: wait time over 20s,
    visibility over 12h, delay over 15 minutes, a batch over 10 entries.
    """

    status_code = 400
    error_code = "InvalidParameterValue"
    message = "invalid value for a parameter"


class InvalidMessageContents(AppError):
    """The message body is not valid — bad characters, or over the size cap."""

    status_code = 400
    error_code = "InvalidMessageContents"
    message = "the message contains characters outside the allowed set"


class ReceiptHandleIsInvalid(AppError):
    """The receipt handle cannot be parsed, or was never issued by this node.

    Deliberately distinct from a *superseded* handle. This one means the caller
    is sending something you did not mint — a bug, or someone probing. V1's
    forgery criterion is graded here.
    """

    status_code = 400
    error_code = "ReceiptHandleIsInvalid"
    message = "the receipt handle is not valid"


class MessageNotInflight(AppError):
    """`ChangeMessageVisibility` on a message whose lease is over.

    The honest slow worker's error. It is not a forgery and not a bug in the
    caller: it is the visibility timeout doing its job, and a spike in it is a
    signal that the timeout is set shorter than the work takes.
    """

    status_code = 400
    error_code = "MessageNotInflight"
    message = "the message referred to is not in flight"


# --- batches ----------------------------------------------------------------
#
# Note what is *not* here: a per-entry failure. A batch with some bad entries is
# a **200** carrying a `Failed` list, not an error response — the protocol
# checklist grades that distinction. These are only the errors that make the
# whole batch unprocessable.


class EmptyBatchRequest(AppError):
    status_code = 400
    error_code = "EmptyBatchRequest"
    message = "the batch request does not contain any entries"


class InvalidBatchEntryId(AppError):
    status_code = 400
    error_code = "InvalidBatchEntryId"
    message = "a batch entry id contains an invalid character or is too long"


class BatchEntryIdsNotDistinct(AppError):
    status_code = 400
    error_code = "BatchEntryIdsNotDistinct"
    message = "two or more batch entries have the same id"


# --- quotas and auth --------------------------------------------------------


class OverLimit(AppError):
    """A quota was hit: in-flight messages, queues, waiters, dedup entries.

    Retryable, because the limit is about *concurrent* state: the caller can get
    in once somebody else's messages are deleted. This is the queue applying
    backpressure, and answering it correctly is what stops one consumer that
    never deletes anything from taking the node down.
    """

    status_code = 403
    error_code = "OverLimit"
    message = "a service quota was exceeded"
    retryable = True


class RequestThrottled(AppError):
    """Rate limited."""

    status_code = 429
    error_code = "RequestThrottled"
    message = "the request was throttled"
    retryable = True


class AccessDenied(AppError):
    """SigV4 verified, but the principal may not perform this action here.

    Sending to a queue and receiving from it are different permissions — the
    security checklist grades that they are checked separately, per queue.
    """

    status_code = 403
    error_code = "AccessDeniedException"
    message = "access denied"


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map an `AppError` to an AWS-shaped error body."""
    if not isinstance(exc, AppError):  # pragma: no cover - registry invariant
        raise exc

    message = exc.message
    if exc.status_code >= 500:
        # Never hand an instance message to a caller on this path — it may carry
        # an internal detail, and on this service that could include a queue name
        # or a receipt handle the caller had no business seeing. The detail goes
        # to the log; the caller gets the class default, which we authored.
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
    """Anything unexpected becomes a 500 with nothing in it.

    Worth being deliberate about on a queue: the dangerous accident here is not a
    500, it is a 200. A `SendMessage` that answers success after failing to
    enqueue has silently dropped a customer's message, and no retry will ever
    happen because the client was told it worked. When in doubt, fail loudly.
    """
    log.error("unhandled exception", error=str(exc), kind=type(exc).__name__)
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
