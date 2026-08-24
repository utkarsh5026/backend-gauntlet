"""The control/invoke plane, shaped like the real Lambda API.

The paths carry AWS's date-stamped API versions (`/2015-03-31/functions/...`)
because mirroring them is the point: everything you learn about this surface
transfers to the real service, and a client written for one works against the
other.

Routing, validation and the sequencing of an invocation are wired. The steps
themselves call into the verticals, which raise until you build them — that is the
worklist.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_pascal

from .errors import FUNCTION_ERROR_HEADER, InvalidRequestContent, RequestTooLarge, TooManyRequests
from .event_source import EventSourceMapping, StartingPosition
from .models import FunctionConfig, InvocationResult, InvocationType, Outcome
from .state import AppState

__all__ = ["public_router"]

log = structlog.get_logger(__name__)


class WireModel(BaseModel):
    """Accepts AWS's PascalCase on the wire, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_pascal, populate_by_name=True)


class CreateFunctionRequest(WireModel):
    function_name: str
    handler: str
    memory_size: int | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)
    environment: dict[str, str] = {}
    reserved_concurrent_executions: int | None = Field(default=None, ge=0)
    provisioned_concurrent_executions: int = Field(default=0, ge=0)


class ConcurrencyRequest(WireModel):
    reserved_concurrent_executions: int | None = Field(default=None, ge=0)


class CreateMappingRequest(WireModel):
    function_name: str
    event_source_arn: str
    batch_size: int | None = Field(default=None, gt=0)
    maximum_batching_window_in_seconds: float | None = Field(default=None, gt=0)
    starting_position: StartingPosition = StartingPosition.TRIM_HORIZON
    parallelization_factor: int | None = Field(default=None, ge=1)


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
# The real header. Absent means RequestResponse — synchronous is the default.
InvocationTypeHeader = Annotated[str | None, Header(alias="X-Amz-Invocation-Type")]

public_router = APIRouter()


@public_router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@public_router.get("/2015-03-31/functions")
async def list_functions(state: StateDep) -> dict[str, list[dict[str, Any]]]:
    """Which functions this node serves. Plumbing, and the first thing you'll curl."""
    return {
        "Functions": [
            {
                "FunctionName": name,
                "FunctionArn": state.registry.get(name).arn,
                "Handler": state.registry.get(name).handler,
                "MemorySize": state.registry.get(name).memory_mb,
                "Timeout": state.registry.get(name).timeout_seconds,
            }
            for name in state.registry.names()
        ]
    }


@public_router.post("/2015-03-31/functions", status_code=201)
async def create_function(body: CreateFunctionRequest, state: StateDep) -> dict[str, Any]:
    """Register a function. Plumbing — the execution behaviour is V1-V3's."""
    settings = state.settings
    function = FunctionConfig(
        name=body.function_name,
        handler=body.handler,
        memory_mb=body.memory_size or settings.default_memory_mb,
        timeout_seconds=body.timeout or settings.default_timeout_seconds,
        environment=dict(body.environment),
        reserved_concurrency=body.reserved_concurrent_executions,
        provisioned_concurrency=body.provisioned_concurrent_executions,
    )
    state.registry.register(function)
    log.info(
        "function registered",
        function=function.name,
        memory_mb=function.memory_mb,
        timeout_seconds=function.timeout_seconds,
    )
    return {
        "FunctionName": function.name,
        "FunctionArn": function.arn,
        "State": "Active",
    }


@public_router.put("/2017-10-31/functions/{function_name}/concurrency")
async def put_concurrency(
    function_name: Annotated[str, Path()], body: ConcurrencyRequest, state: StateDep
) -> dict[str, Any]:
    """Reserve (or clear) concurrency for a function.

    Wired through to V4's governor, which is where the validation that matters
    lives — reserving more than the account has left must be refused.
    """
    state.registry.get(function_name)
    state.governor.set_reserved(function_name, body.reserved_concurrent_executions)
    return {"ReservedConcurrentExecutions": body.reserved_concurrent_executions}


@public_router.post("/2015-03-31/functions/{function_name}/invocations")
async def invoke(
    function_name: Annotated[str, Path()],
    request: Request,
    state: StateDep,
    x_amz_invocation_type: InvocationTypeHeader = None,
) -> Response:
    """Invoke a function. The endpoint the whole project exists to serve.

    The sequencing below is the wiring; every numbered step is a vertical.
    """
    function = state.registry.get(function_name)
    payload = await request.body()

    try:
        invocation_type = InvocationType(x_amz_invocation_type or InvocationType.REQUEST_RESPONSE)
    except ValueError as exc:
        raise InvalidRequestContent(f"unknown invocation type {x_amz_invocation_type!r}") from exc

    if invocation_type is InvocationType.DRY_RUN:
        # Validate and stop. Nothing to build here — it exists so the invocation
        # type enum is honest.
        return Response(status_code=204)

    if invocation_type is InvocationType.EVENT:
        # V5 owns the async path end to end: bound-check, enqueue, return 202
        # before the handler has run.
        request_id = await state.async_queue.enqueue(function, payload)
        return JSONResponse(status_code=202, content=None, headers={"x-amzn-requestid": request_id})

    if len(payload) > state.settings.max_sync_payload_bytes:
        raise RequestTooLarge(
            f"payload of {len(payload)} bytes exceeds the "
            f"{state.settings.max_sync_payload_bytes} byte synchronous limit"
        )

    # --- the synchronous path ------------------------------------------------
    # 1. V4: take a concurrency slot, or throttle. Never queue.
    lease = state.governor.try_acquire(function)
    if lease is None:
        raise TooManyRequests(f"no concurrency available for {function.name!r}")

    async with lease:
        # 2. V2: get an environment — warm if one is idle, cold otherwise. On the
        #    cold path this is also where V4's scale-up pacing applies, because
        #    creating an environment is the thing that is rate-limited, not the
        #    invocation itself.
        environment, cold = await state.pool.acquire(function)
        healthy = True
        try:
            # 3. V1: hand the invocation to whichever runtime is polling this
            #    environment, and wait for it to post a result.
            result = await _submit(state, function, environment.environment_id, payload, cold=cold)
        except Exception:
            # The environment is suspect — V2 decides whether it can be reused, and
            # this is the input to that decision.
            healthy = False
            raise
        finally:
            # 4. V2: freeze it for reuse, or retire it.
            await state.pool.release(environment, healthy=healthy)

    return _invocation_response(result)


async def _submit(
    state: AppState,
    function: FunctionConfig,
    environment_id: str,
    payload: bytes,
    *,
    cold: bool,
) -> InvocationResult:
    """Build the invocation and hand it to the broker.

    Split out so the async worker (V5) submits along exactly the same path as a
    synchronous caller — two code paths that "both invoke" are two places for the
    delivery semantics to drift apart.
    """
    from .models import Invocation

    invocation = Invocation(function=function, payload=payload)
    # TODO(V1): the broker needs to know which environment this invocation is
    # destined for, so `/next` hands it to that environment's poller and no other.
    # That routing is what makes V3's "an environment cannot poll another's queue"
    # enforceable rather than aspirational.
    _ = (environment_id, cold)
    return await state.broker.submit(invocation)


def _invocation_response(result: InvocationResult) -> Response:
    """Turn a result into the response shape the real API returns.

    The load-bearing detail: a **function error is still a 200**, marked by
    `X-Amz-Function-Error`. From the platform's point of view the invocation
    succeeded — it ran the code, and the code threw.
    """
    headers = {
        "x-amzn-requestid": result.request_id,
        # Not part of the real API; here because the SPEC asks for cold/warm to be
        # observable per invocation rather than inferred from a duration threshold.
        "x-lambda-cold-start": "true" if result.cold else "false",
    }
    if result.outcome is Outcome.FUNCTION_ERROR:
        headers[FUNCTION_ERROR_HEADER] = result.error_type or "Unhandled"
    return Response(
        content=result.payload,
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


@public_router.post("/2015-03-31/event-source-mappings", status_code=202)
async def create_event_source_mapping(
    body: CreateMappingRequest, state: StateDep
) -> dict[str, Any]:
    """Bind a stream to a function. The poller itself is V6's.

    Registering the mapping is plumbing; note that it is registered but NOT
    started here — `main`'s lifespan owns poller tasks, so shutdown has exactly one
    place to stop them.
    """
    function = state.registry.get(body.function_name)
    settings = state.settings
    mapping = EventSourceMapping(
        uuid=str(uuid.uuid4()),
        function=function,
        source_url=body.event_source_arn or settings.event_source_url,
        batch_size=body.batch_size or settings.event_source_batch_size,
        batch_window_seconds=(
            body.maximum_batching_window_in_seconds or settings.event_source_batch_window_seconds
        ),
        starting_position=body.starting_position,
        parallelisation_factor=(
            body.parallelization_factor or settings.event_source_parallelisation
        ),
    )
    state.mappings[mapping.uuid] = mapping
    return {
        "UUID": mapping.uuid,
        "FunctionArn": function.arn,
        "State": mapping.state.value,
        "BatchSize": mapping.batch_size,
    }


@public_router.get("/2015-03-31/event-source-mappings")
async def list_event_source_mappings(state: StateDep) -> dict[str, list[dict[str, Any]]]:
    return {
        "EventSourceMappings": [
            {
                "UUID": mapping.uuid,
                "FunctionArn": mapping.function.arn,
                "State": mapping.state.value,
                "BatchSize": mapping.batch_size,
            }
            for mapping in state.mappings.values()
        ]
    }
