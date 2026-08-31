"""The gRPC surface: a thin adapter between protobuf messages and the engine.

Deliberately thin. Everything interesting is behind `Dispatcher`; this file's job
is to unpack a request, validate what can be validated *before* the store is
touched, hand it to the dispatcher, and turn the answer (or the exception) back
into something the wire understands. If logic starts accumulating here, it
belongs in a vertical module instead.

Wired and working as scaffolding: every RPC validates, dispatches, and maps
errors to the right status code, and the smoke tests hold that contract. The
`TODO(horizontal)` markers are SPEC checklist items — deadlines, structured logs,
spans — that you weave in as you go. They are plain comments, not
`NotImplementedError`, because the service *runs*: it is the engine underneath it
that is still a worklist.

Two contracts the SPEC grades live here rather than in the engine:

* **An empty poll response is not an error.** A long-poll that finds no work
  returns a default-constructed response with an empty `task_token`, and the
  worker polls again. Returning `DEADLINE_EXCEEDED` or `NOT_FOUND` instead would
  make every idle worker log an error every five seconds and would teach
  client-side retry logic to back off from a perfectly healthy engine.
* **Validation happens at the edge.** An empty task queue, a non-UUID run id, a
  malformed token, an unknown command type, an oversize payload — all rejected
  with `INVALID_ARGUMENT` before a connection is taken from the pool. Garbage
  should never cost a round-trip to Postgres.
"""

from __future__ import annotations

import json
from uuid import UUID

import grpc
import grpc.aio
import structlog

from .dispatch import Dispatcher
from .errors import AppError, Internal, InvalidArgument, abort
from .history import StartOptions
from .model import (
    Command,
    CompleteWorkflow,
    Event,
    ExecutionStatus,
    FailWorkflow,
    ScheduleActivity,
    StartTimer,
    TaskToken,
)
from .pb import workflow_pb2 as pb
from .pb import workflow_pb2_grpc as rpc

__all__ = ["WorkflowService", "decode_command", "to_pb_event"]

log = structlog.get_logger(__name__)

# `type` statements (PEP 695), not plain assignment: a bare `X = SomeType[...]`
# is a *variable* as far as a type checker is concerned, and using it in an
# annotation is an error under strict mode. `ServicerContext` is invariant in
# both parameters, so each RPC needs its own alias rather than one widened one.
type StartCtx = grpc.aio.ServicerContext[pb.StartWorkflowRequest, pb.StartWorkflowResponse]
type PollWfCtx = grpc.aio.ServicerContext[pb.PollWorkflowTaskRequest, pb.PollWorkflowTaskResponse]
type RespondWfCtx = grpc.aio.ServicerContext[
    pb.RespondWorkflowTaskCompletedRequest, pb.RespondWorkflowTaskCompletedResponse
]
type PollActCtx = grpc.aio.ServicerContext[pb.PollActivityTaskRequest, pb.PollActivityTaskResponse]
type RespondActCtx = grpc.aio.ServicerContext[
    pb.RespondActivityTaskCompletedRequest, pb.RespondActivityTaskCompletedResponse
]
type FailActCtx = grpc.aio.ServicerContext[
    pb.RespondActivityTaskFailedRequest, pb.RespondActivityTaskFailedResponse
]
type ResultCtx = grpc.aio.ServicerContext[pb.GetWorkflowResultRequest, pb.GetWorkflowResultResponse]


# ---- protobuf ⇄ model marshaling (wiring, not logic) ------------------------


def to_pb_event(event: Event) -> pb.HistoryEvent:
    """Render an internal `Event` onto the wire.

    The internal `EventType` members are named exactly like the proto enum's
    values, so this is one lookup rather than the twelve-arm match the same code
    needs in a language without runtime enum reflection — and it fails loudly
    (`ValueError` from `Value()`) if the two ever drift apart, instead of
    compiling into a silent hole.
    """
    return pb.HistoryEvent(
        event_id=event.event_id,
        event_type=pb.EventType.Value(event.event_type.name),
        timestamp_ms=event.timestamp_ms,
        attributes=json.dumps(event.attributes).encode(),
    )


def decode_command(cmd: pb.Command, max_payload: int) -> Command:
    """Decode one wire command into the internal `Command` union.

    Rejects anything malformed before it reaches the engine. proto3 has no
    required fields and no optional scalars, so "missing" arrives as an empty
    string / zero — which means *this* function is the only thing standing
    between a command with no activity type and a history event that records one.
    """
    match cmd.command_type:
        case pb.SCHEDULE_ACTIVITY:
            if not cmd.activity_type:
                raise InvalidArgument("activity_type must not be empty")
            _check_payload(cmd.activity_input, max_payload, "activity_input")
            return ScheduleActivity(activity_type=cmd.activity_type, input=cmd.activity_input)
        case pb.START_TIMER:
            if not cmd.timer_id:
                raise InvalidArgument("timer_id must not be empty")
            if cmd.timer_delay_ms < 0:
                raise InvalidArgument("timer_delay_ms must not be negative")
            return StartTimer(timer_id=cmd.timer_id, delay_ms=cmd.timer_delay_ms)
        case pb.COMPLETE_WORKFLOW:
            _check_payload(cmd.result, max_payload, "result")
            return CompleteWorkflow(result=cmd.result)
        case pb.FAIL_WORKFLOW:
            _check_payload(cmd.failure.encode(), max_payload, "failure")
            return FailWorkflow(failure=cmd.failure)
        case pb.COMMAND_TYPE_UNSPECIFIED:
            raise InvalidArgument("command type is unspecified")
        case _:
            raise InvalidArgument("unknown command type")


def _check_payload(payload: bytes, max_payload: int, field: str) -> None:
    """Enforce the payload cap.

    Payloads are opaque bytes the engine stores and hands on — it never decodes
    or executes them — so the only thing to say about one is how big it is
    allowed to be. Unbounded, a single RPC can write an arbitrarily large row
    into an append-only table that is never garbage-collected.
    """
    if len(payload) > max_payload:
        raise InvalidArgument(f"{field} exceeds the {max_payload} byte limit")


def _parse_run_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise InvalidArgument("run_id is not a valid uuid") from None


def _require_token(raw: bytes) -> TaskToken:
    token = TaskToken.decode(raw)
    if token is None:
        # INVALID_ARGUMENT, never INTERNAL: a token that does not parse is the
        # caller's problem, and the SPEC grades telling them apart.
        raise InvalidArgument("malformed task token")
    return token


class WorkflowService(rpc.WorkflowServiceServicer):
    """Implements `workflow.v1.WorkflowService`."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._max_payload = dispatcher.settings.max_payload_bytes

    async def StartWorkflow(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.StartWorkflowRequest,
        context: StartCtx,
    ) -> pb.StartWorkflowResponse:
        """Open an execution and return the run id that names this attempt."""
        try:
            if not request.task_queue:
                raise InvalidArgument("task_queue must not be empty")
            if not request.workflow_type:
                raise InvalidArgument("workflow_type must not be empty")
            _check_payload(request.input, self._max_payload, "input")
            run_id = await self._dispatcher.start_workflow(
                StartOptions(
                    workflow_id=request.workflow_id,
                    workflow_type=request.workflow_type,
                    task_queue=request.task_queue,
                    input=request.input,
                )
            )
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            # The scaffold's own state: let it surface untouched so the worklist
            # is obvious, rather than dressing it up as an internal error.
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return pb.StartWorkflowResponse(run_id=str(run_id))

    async def PollWorkflowTask(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.PollWorkflowTaskRequest,
        context: PollWfCtx,
    ) -> pb.PollWorkflowTaskResponse:
        """Long-poll for the next workflow task on `task_queue`."""
        # TODO(horizontal/protocols): honour the client's deadline.
        # `context.time_remaining()` is the seconds left (None if the client set
        # none). Parking for the full server-side long-poll window when the
        # client will give up in one second wastes a slot in
        # `max_concurrent_rpcs` and answers into a socket nobody is reading.
        try:
            if not request.task_queue:
                raise InvalidArgument("task_queue must not be empty")
            task = await self._dispatcher.poll_workflow_task(request.task_queue, request.identity)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))

        if task is None:
            # An empty response, NOT an error status: the long-poll timed out
            # with no work and the worker should simply poll again.
            return pb.PollWorkflowTaskResponse()

        # TODO(horizontal/observability): one structured line per dispatch, with
        # run_id, task kind, sticky hit/miss and events replayed — the four
        # fields that let you answer "why was this task slow?" from logs alone.
        return pb.PollWorkflowTaskResponse(
            task_token=task.token.encode(),
            workflow_id=task.workflow_id,
            run_id=str(task.run_id),
            history=[to_pb_event(e) for e in task.history],
            sticky_cache_hit=task.sticky_cache_hit,
        )

    async def RespondWorkflowTaskCompleted(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.RespondWorkflowTaskCompletedRequest,
        context: RespondWfCtx,
    ) -> pb.RespondWorkflowTaskCompletedResponse:
        """Apply the commands the workflow produced for one task."""
        try:
            token = _require_token(request.task_token)
            commands = [decode_command(c, self._max_payload) for c in request.commands]
            await self._dispatcher.complete_workflow_task(token, commands)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return pb.RespondWorkflowTaskCompletedResponse()

    async def PollActivityTask(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.PollActivityTaskRequest,
        context: PollActCtx,
    ) -> pb.PollActivityTaskResponse:
        """Long-poll for the next activity task on `task_queue`."""
        try:
            if not request.task_queue:
                raise InvalidArgument("task_queue must not be empty")
            task = await self._dispatcher.poll_activity_task(request.task_queue, request.identity)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))

        if task is None:
            return pb.PollActivityTaskResponse()
        return pb.PollActivityTaskResponse(
            task_token=task.token.encode(),
            activity_type=task.activity_type,
            input=task.input,
            workflow_id=task.workflow_id,
            run_id=str(task.run_id),
        )

    async def RespondActivityTaskCompleted(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.RespondActivityTaskCompletedRequest,
        context: RespondActCtx,
    ) -> pb.RespondActivityTaskCompletedResponse:
        """Record an activity's result and wake its workflow."""
        try:
            token = _require_token(request.task_token)
            _check_payload(request.result, self._max_payload, "result")
            await self._dispatcher.complete_activity_task(token, request.result)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return pb.RespondActivityTaskCompletedResponse()

    async def RespondActivityTaskFailed(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.RespondActivityTaskFailedRequest,
        context: FailActCtx,
    ) -> pb.RespondActivityTaskFailedResponse:
        """Record an activity failure and wake its workflow."""
        try:
            token = _require_token(request.task_token)
            _check_payload(request.failure.encode(), self._max_payload, "failure")
            await self._dispatcher.fail_activity_task(token, request.failure)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return pb.RespondActivityTaskFailedResponse()

    async def GetWorkflowResult(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.GetWorkflowResultRequest,
        context: ResultCtx,
    ) -> pb.GetWorkflowResultResponse:
        """Report an execution's current state — terminal result when done."""
        try:
            run_id = _parse_run_id(request.run_id)
            state = await self._dispatcher.get_result(run_id)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return pb.GetWorkflowResultResponse(
            running=state.status is ExecutionStatus.RUNNING,
            completed=state.status is ExecutionStatus.COMPLETED,
            result=state.result or b"",
            failure=state.failure or "",
        )
