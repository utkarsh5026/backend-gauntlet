"""Which wire shape a service speaks, and the two headers every one of them sets.

AWS is not one protocol, it is four, and a service's age tells you which it got.
The Query protocol (form-encoded request, XML response, verb in an `Action`
parameter) is what IAM and STS still speak — project **25**. AWS JSON (one
endpoint, verb in an `X-Amz-Target` header, JSON document body) is what DynamoDB,
Lambda's control plane and modern SQS speak — projects **23**, **24**, **29**.
The shift is worth noticing rather than papering over: the verb moved out of the
body and into a header, and the body stopped being a flattened parameter list and
became a document.

This module is the leaf of the package — it imports nothing else here — because
both the error renderer and the dispatcher need to know the shape, and neither
should have to import the other to find out.
"""

from __future__ import annotations

import enum
from uuid import uuid4

__all__ = [
    "REQUEST_ID_HEADER",
    "TARGET_HEADER",
    "WireProtocol",
    "new_request_id",
]

# Lower-case because every ASGI server hands headers over lower-cased; comparing
# against a mixed-case literal is the bug that makes a header "randomly" missing.
TARGET_HEADER = "x-amz-target"

# Not `x-request-id` (that is `common-telemetry`'s, for correlating *our* logs).
# This is the one AWS SDKs read back and quote at you in a support ticket, and
# every AWS error response carries it — including the ones nobody parses.
REQUEST_ID_HEADER = "x-amzn-requestid"


class WireProtocol(enum.StrEnum):
    """The serialization a service speaks, which decides how errors are rendered.

    Named `WireProtocol` rather than `Protocol` on purpose: `typing.Protocol` is
    a thing every one of these modules might reasonably want, and a shared
    package that shadows a stdlib name makes every importer pay for it.
    """

    JSON_1_0 = "json-1.0"
    JSON_1_1 = "json-1.1"
    QUERY = "query"
    REST_JSON = "rest-json"

    @property
    def content_type(self) -> str:
        """The `Content-Type` a *successful* response carries."""
        match self:
            case WireProtocol.JSON_1_0:
                return "application/x-amz-json-1.0"
            case WireProtocol.JSON_1_1:
                return "application/x-amz-json-1.1"
            case WireProtocol.QUERY:
                # The request is form-encoded; the response is XML. The asymmetry
                # is the protocol's, not a mistake here.
                return "text/xml"
            case WireProtocol.REST_JSON:
                return "application/json"

    @property
    def is_json(self) -> bool:
        return self is not WireProtocol.QUERY


def new_request_id() -> str:
    """A fresh request id, in the shape AWS uses (a lower-case UUID).

    Minted per request and echoed in `x-amzn-requestid` on **every** response,
    success or failure. It is the only handle a caller has when they need to ask
    you what happened, so a service that omits it on the error path has removed
    it from exactly the case it exists for.
    """
    return str(uuid4())
