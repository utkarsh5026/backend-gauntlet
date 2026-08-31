"""The envelope is only right if a real SDK agrees, so botocore is the oracle."""

from __future__ import annotations

import json
from typing import Any, cast

import botocore.parsers  # pyright: ignore[reportMissingTypeStubs]
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from common_aws import (
    AccessDenied,
    AwsError,
    Fault,
    InternalFailure,
    ThrottlingException,
    ValidationException,
    WireProtocol,
    error_body,
    error_response,
    install_error_handlers,
)


class Leaky(AwsError):
    """A 5xx whose instance message must never reach a caller."""

    status_code = 500
    error_code = "InternalFailure"
    message = "an internal error occurred"


def _read(response: Response) -> dict[str, Any]:
    return json.loads(bytes(response.body))


def _parse_with(parser: Any, response: Response) -> dict[str, Any]:
    """Run a response through botocore's own parser, the way an SDK call does."""
    parsed = parser.parse(  # pyright: ignore[reportUnknownMemberType]
        {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": bytes(response.body),
        },
        None,
    )
    return cast(dict[str, Any], parsed)


def test_json_envelope_is_what_botocore_parses() -> None:
    response = error_response(ThrottlingException(), protocol=WireProtocol.JSON_1_0)
    parsed = _parse_with(botocore.parsers.JSONParser(), response)
    assert parsed["Error"]["Code"] == "ThrottlingException"
    assert parsed["Error"]["Message"] == "rate exceeded"
    # The request id reaches the SDK, which is what a caller quotes back at you.
    assert parsed["ResponseMetadata"]["RequestId"]


def test_query_envelope_is_what_botocore_parses() -> None:
    response = error_response(AccessDenied(), protocol=WireProtocol.QUERY, request_id="req-1")
    parsed = _parse_with(botocore.parsers.QueryParser(), response)
    assert parsed["Error"] == {"Type": "Sender", "Code": "AccessDenied", "Message": "access denied"}
    assert parsed["ResponseMetadata"]["RequestId"] == "req-1"


def test_rest_json_uses_lambdas_fault_field() -> None:
    body = error_body(ValidationException(), protocol=WireProtocol.REST_JSON, request_id="r")
    assert body == {"Type": "User", "message": "the request is not valid"}


def test_retryable_errors_advertise_a_wait() -> None:
    assert error_response(ThrottlingException()).headers["retry-after"] == "1"
    assert "retry-after" not in error_response(ValidationException()).headers


def test_error_type_header_is_set_even_for_the_json_protocols() -> None:
    # botocore falls back to this header when the body is unparseable — exactly
    # the case where you most want the code to survive.
    assert (
        error_response(ValidationException()).headers["x-amzn-errortype"] == "ValidationException"
    )


def test_a_5xx_never_carries_its_instance_message() -> None:
    leaked = "queue orders-prod row 4412 belongs to account 999"
    response = error_response(Leaky(leaked))
    assert leaked not in json.dumps(_read(response))
    assert _read(response)["message"] == Leaky.message


def test_a_4xx_does_carry_its_instance_message() -> None:
    response = error_response(ValidationException("Key is missing attribute 'pk'"))
    assert _read(response)["message"] == "Key is missing attribute 'pk'"


def test_fault_is_derived_from_status_and_overridable() -> None:
    assert ValidationException().sender_fault is True
    assert InternalFailure().sender_fault is False

    class OurFault(AwsError):
        status_code = 400
        error_code = "OurFault"
        fault = Fault.SERVER

    assert OurFault().sender_fault is False
    assert OurFault().resolved_fault.query_name == "Receiver"


def test_subclasses_keep_the_field_names_the_projects_already_use() -> None:
    # The adoption path for 23/24/25/29 is deleting their base class, not
    # rewriting every subclass — this asserts that stays true.
    class QueueDoesNotExist(AwsError):
        status_code = 400
        error_code = "QueueDoesNotExist"
        message = "the specified queue does not exist"
        retryable = False

    body = error_body(QueueDoesNotExist(), protocol=WireProtocol.JSON_1_0, request_id="r")
    assert body == {"__type": "QueueDoesNotExist", "message": "the specified queue does not exist"}


async def test_installed_handler_renders_a_raised_error() -> None:
    async def boom(_request: Request) -> Response:
        raise ValidationException("no partition key in the request")

    async def fine(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    routes = [Route("/boom", boom, methods=["POST"]), Route("/fine", fine, methods=["POST"])]
    app = Starlette(routes=routes)
    install_error_handlers(app, protocol=WireProtocol.JSON_1_0)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        response = await client.post("/boom")
        assert response.status_code == 400
        assert response.json()["__type"] == "ValidationException"
        assert response.headers["x-amzn-requestid"]
        assert (await client.post("/fine")).json() == {"ok": True}


def test_the_catch_all_is_registered_and_renders_an_opaque_500() -> None:
    # Registration is asserted rather than exercised: Starlette re-raises after
    # the handler runs, so under a test transport the exception propagates. That
    # is the property the scaffolds rely on to assert on `NotImplementedError`.
    app = Starlette()
    install_error_handlers(app)
    assert Exception in app.exception_handlers
    assert AwsError in app.exception_handlers

    response = error_response(InternalFailure())
    assert response.status_code == 500
    assert _read(response) == {"__type": "InternalFailure", "message": InternalFailure.message}
