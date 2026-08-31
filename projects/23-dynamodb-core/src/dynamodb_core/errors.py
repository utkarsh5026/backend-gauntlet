"""DynamoDB's named exceptions, mapped to HTTP responses.

Handlers raise these; one handler does the mapping, so status-code policy lives in
exactly one place. The names match the real service so the mental model transfers —
if you have ever retried a `ProvisionedThroughputExceededException` in production,
this is the thing that was throwing it.

Each carries `retryable`, which is the distinction that actually matters to a
client: a throttle means *come back shortly*, a failed condition means *your
assumption was wrong, retrying unchanged will fail again*. A client that retries
the second one is a client in an infinite loop.

Note: the real service returns HTTP 400 for nearly all of these and distinguishes
them only by the error name in the body. This scaffold uses HTTP-meaningful codes
instead (409 for a conflict, 429 for a throttle); if you'd rather mirror AWS
exactly, that's a legitimate change — record whichever you pick in the design doc.

The *envelope* — how an error is spelled on the wire, the `retry-after` header, the
rule that a 5xx never carries its instance message — is `common-aws`, shared with
every service in the tier. What stays here is the part that is DynamoDB's: which
errors exist, what each one means, and what it costs the caller.
"""

from __future__ import annotations

from common_aws import AwsError, WireProtocol
from common_aws import install_error_handlers as install_aws_error_handlers
from fastapi import FastAPI

__all__ = [
    "AppError",
    "ConditionalCheckFailed",
    "ItemCollectionSizeLimitExceeded",
    "ProvisionedThroughputExceeded",
    "ResourceNotFound",
    "TransactionCanceled",
    "ValidationError",
    "install_error_handlers",
]


class AppError(AwsError):
    """Base for every error this service turns into a response.

    A thin subclass of the shared `AwsError`: the fields, the wire rendering and
    the safety rules come from `common-aws`. What is DynamoDB's — and the reason
    this class still exists rather than being an import — is the code the service
    answers with when something unexpected happens. The real service says
    `InternalServerError`; Lambda says `ServiceException`; a caller's retry logic
    reads it.
    """

    error_code = "InternalServerError"
    message = "internal server error"


class ValidationError(AppError):
    """Malformed request: missing key attribute, illegal key type, bad expression."""

    status_code = 400
    error_code = "ValidationException"
    message = "invalid request"


class ResourceNotFound(AppError):
    """No such table or index."""

    status_code = 404
    error_code = "ResourceNotFoundException"
    message = "requested resource not found"


class ConditionalCheckFailed(AppError):
    """A ConditionExpression evaluated false (V3).

    Deliberately **not** retryable: the write was correctly refused, and the caller
    must re-read and decide again.
    """

    status_code = 409
    error_code = "ConditionalCheckFailedException"
    message = "the conditional request failed"


class ProvisionedThroughputExceeded(AppError):
    """Capacity exhausted (V4) — the one clients are expected to back off and retry."""

    status_code = 429
    error_code = "ProvisionedThroughputExceededException"
    message = "throughput exceeds the current capacity for this table or partition"
    retryable = True


class TransactionCanceled(AppError):
    """A transaction leg failed, so none of it applied (V3)."""

    status_code = 409
    error_code = "TransactionCanceledException"
    message = "transaction cancelled"


class ItemCollectionSizeLimitExceeded(AppError):
    """The item exceeds the configured per-item cap (V1)."""

    status_code = 413
    error_code = "ItemCollectionSizeLimitExceededException"
    message = "item size exceeds the maximum allowed"


def install_error_handlers(app: FastAPI) -> None:
    """Wire the shared renderer up to this service's protocol and base error.

    DynamoDB speaks **AWS JSON 1.0**: `{"__type": …, "message": …}` with the code
    repeated in `x-amzn-errortype`, which is what an SDK reads to decide whether
    to retry.
    """
    install_aws_error_handlers(app, protocol=WireProtocol.JSON_1_0, internal_error=AppError)
