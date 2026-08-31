"""The AWS wire envelope, shared by every Tier 8 service.

Fully implemented on purpose: CLAUDE.md marks the `common-*` helpers as the one
exception to "the owner writes the interesting code". Projects 23, 24, 25 and 29
had each grown their own copy of the same four things — an `AppError` base, an
error renderer, a target parser, a batch envelope — which is three copies too
many of code whose only job is to be identical to AWS.

The line this package draws is **envelope vs. meaning**. The envelope is how a
request names an operation, how a document is flattened onto the wire, and how a
failure is spelled so an SDK knows what to do next; it is the same for every
service and it lives here. The meaning — what a receipt handle is, when a
conditional write fails, whether this principal may call that action — is the
service's, and every bit of it stays in the project that is graded on it.

  * `wire`        — which protocol a service speaks; the headers all of them set.
  * `errors`      — `AwsError`, the codes every service returns, and rendering.
  * `dispatch`    — `X-Amz-Target` parsing and the action -> handler table.
  * `batch`       — the two-list partial-failure envelope.
  * `query`       — the Query protocol: flattened parameters in, XML out.
  * `arn`         — parsing the name services pass to each other.
  * `pagination`  — signed, opaque continuation tokens.

**What is deliberately absent: SigV4.** Verifying a signature — canonicalization,
the four chained HMACs, the skew window, replay, constant-time comparison — is
project 25's V1, and shipping it here would hand that vertical away. Services
answer with the auth error codes in `errors`; deciding *whether* to answer with
them is 25's, over HTTP, which is also how the real split works.

Adopting it in an existing service is small, because the field names were chosen
to match what those projects already wrote:

    from common_aws import AwsError, WireProtocol, install_error_handlers

    class QueueDoesNotExist(AwsError):        # was: AppError
        status_code = 400
        error_code = "QueueDoesNotExist"
        message = "the specified queue does not exist"

    install_error_handlers(app, protocol=WireProtocol.JSON_1_0)
"""

from __future__ import annotations

from .arn import Arn
from .batch import (
    MAX_BATCH_ENTRIES,
    MAX_BATCH_ENTRY_ID_LENGTH,
    BatchEntry,
    BatchResult,
    parse_batch_entries,
)
from .dispatch import TargetDispatcher, parse_target
from .errors import (
    AccessDenied,
    AwsError,
    ExpiredToken,
    Fault,
    IncompleteSignature,
    InternalFailure,
    InvalidAction,
    InvalidClientTokenId,
    InvalidParameterValue,
    MissingAction,
    MissingAuthenticationToken,
    MissingParameter,
    ResourceNotFound,
    SerializationException,
    ServiceUnavailable,
    SignatureDoesNotMatch,
    ThrottlingException,
    ValidationException,
    current_request_id,
    error_body,
    error_response,
    install_error_handlers,
)
from .pagination import CursorCodec
from .query import parse_query_params, parse_query_string, render_query_response, require_action
from .wire import REQUEST_ID_HEADER, TARGET_HEADER, WireProtocol, new_request_id

__all__ = [
    "MAX_BATCH_ENTRIES",
    "MAX_BATCH_ENTRY_ID_LENGTH",
    "REQUEST_ID_HEADER",
    "TARGET_HEADER",
    "AccessDenied",
    "Arn",
    "AwsError",
    "BatchEntry",
    "BatchResult",
    "CursorCodec",
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
    "TargetDispatcher",
    "ThrottlingException",
    "ValidationException",
    "WireProtocol",
    "current_request_id",
    "error_body",
    "error_response",
    "install_error_handlers",
    "new_request_id",
    "parse_batch_entries",
    "parse_query_params",
    "parse_query_string",
    "parse_target",
    "render_query_response",
    "require_action",
]
