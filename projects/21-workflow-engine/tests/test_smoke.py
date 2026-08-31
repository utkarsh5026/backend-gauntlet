"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V5. They assert the plumbing
(the server binds, reflection and health answer, validation runs at the edge, the
wire marshaling is right, metrics render) and they pin the scaffold's contract:
a real request raises until you build the engine. When you implement V4's happy
path, the last test here is the first thing that should fail — delete it then.

Note what these prove *about the SPEC*, not just about the code: the
"gRPC contract is deliberate" and "input validation at the frontend" checklist
items are both testable today, because both live at the edge.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import grpc
import grpc.aio
import httpx
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from workflow_engine.config import Settings
from workflow_engine.model import Event, EventType, TaskKind, TaskToken
from workflow_engine.pb import workflow_pb2 as pb
from workflow_engine.pb import workflow_pb2_grpc as rpc
from workflow_engine.service import to_pb_event


def _token() -> bytes:
    """A well-formed token for a run that does not exist.

    Enough to get past `_require_token` and reach the validation being tested.
    """
    return TaskToken(run_id=uuid4(), kind=TaskKind.WORKFLOW, scheduled_event_id=1).encode()


# ---- the admin surface -----------------------------------------------------


async def test_healthz(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_metrics_endpoint_renders(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_graded_metrics_exist_before_anything_happens(
    admin: httpx.AsyncClient,
) -> None:
    """A closed label set must render at zero, not be absent until first use.

    `prometheus_client` emits nothing for a labelled collector whose children do
    not exist yet, so a dashboard on a fresh engine would read "No data" rather
    than 0. `metrics.py` pre-creates every value of the closed label sets for
    exactly this reason. The `task_queue`-labelled series cannot be
    pre-created — that label is caller-supplied — so they are not asserted here.
    """
    body = (await admin.get("/metrics")).text
    for series in (
        'workflow_replays_total{sticky="hit"}',
        'workflow_replays_total{sticky="miss"}',
        'workflow_executions_completed_total{outcome="completed"}',
        "workflow_timers_fired_total",
        "workflow_nondeterminism_total",
        "workflow_dispatch_latency_seconds_bucket",
    ):
        assert series in body


async def test_grpc_health_reports_serving(grpc_channel: grpc.aio.Channel) -> None:
    """`grpc.health.v1` is what a gRPC load balancer checks — it must answer."""
    stub = health_pb2_grpc.HealthStub(grpc_channel)
    response = await stub.Check(health_pb2.HealthCheckRequest(service=""))
    assert response.status == health_pb2.HealthCheckResponse.SERVING


# ---- validation at the edge ------------------------------------------------


async def test_empty_task_queue_is_invalid_argument(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
) -> None:
    """Validation runs before the store, so garbage never costs a round-trip."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.StartWorkflow(
            pb.StartWorkflowRequest(workflow_type="order", workflow_id="o-1", task_queue="")
        )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_non_uuid_run_id_is_invalid_argument(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.GetWorkflowResult(pb.GetWorkflowResultRequest(run_id="not-a-uuid"))
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_malformed_task_token_is_invalid_argument(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
) -> None:
    """A bad token is the caller's bug — INVALID_ARGUMENT, never INTERNAL."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.RespondWorkflowTaskCompleted(
            pb.RespondWorkflowTaskCompletedRequest(task_token=b"not-a-token")
        )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_unspecified_command_type_is_invalid_argument(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
) -> None:
    """proto3 has no required fields: an unset command_type arrives as 0."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.RespondWorkflowTaskCompleted(
            pb.RespondWorkflowTaskCompletedRequest(task_token=_token(), commands=[pb.Command()])
        )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_command_missing_its_required_field_is_invalid_argument(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
) -> None:
    """SCHEDULE_ACTIVITY without an activity_type must not reach history."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.RespondWorkflowTaskCompleted(
            pb.RespondWorkflowTaskCompletedRequest(
                task_token=_token(),
                commands=[pb.Command(command_type=pb.SCHEDULE_ACTIVITY)],
            )
        )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_oversize_payload_is_rejected(
    grpc_stub: rpc.WorkflowServiceAsyncStub,
    settings: Settings,
) -> None:
    """Payloads are opaque, but they are not unbounded."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.StartWorkflow(
            pb.StartWorkflowRequest(
                workflow_type="order",
                workflow_id="o-1",
                task_queue="orders",
                input=b"x" * (settings.max_payload_bytes + 1),
            )
        )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ---- the vocabulary and the wire -------------------------------------------


def test_event_type_is_its_own_column_value() -> None:
    """The StrEnum member *is* the string stored in `history_events`."""
    assert EventType.TIMER_FIRED == "timer_fired"
    assert EventType("workflow_completed") is EventType.WORKFLOW_COMPLETED
    assert EventType.WORKFLOW_COMPLETED.is_terminal
    assert not EventType.ACTIVITY_SCHEDULED.is_terminal


def test_event_marshals_onto_the_wire() -> None:
    """Internal enum member names line up with the proto enum's value names."""
    event = Event(
        event_id=7,
        event_type=EventType.ACTIVITY_COMPLETED,
        timestamp_ms=1_700_000_000_000,
        attributes={"scheduled_event_id": 5},
    )
    wire = to_pb_event(event)
    assert wire.event_id == 7
    assert wire.event_type == pb.ACTIVITY_COMPLETED
    assert wire.timestamp_ms == 1_700_000_000_000
    assert b"scheduled_event_id" in wire.attributes


def test_task_token_round_trips() -> None:
    token = TaskToken(run_id=uuid4(), kind=TaskKind.ACTIVITY, scheduled_event_id=42)
    assert TaskToken.decode(token.encode()) == token


def test_task_token_rejects_garbage() -> None:
    """An empty token is a timed-out poll echoed back; both decode to None."""
    assert TaskToken.decode(b"") is None
    assert TaskToken.decode(b"{}") is None
    assert TaskToken.decode(b'{"run_id": "nope", "kind": "workflow"}') is None


# ---- the durable schema ----------------------------------------------------


async def test_migrated_schema_is_present(pg_pool: asyncpg.Pool[asyncpg.Record]) -> None:
    """The `pg_pool` harness itself, proven — skips when Postgres is not up.

    Every V1/V3/V4 proof runs on this fixture, so it is worth one test that fails
    loudly if template cloning or the migration runner breaks. It also pins the
    four tables the SPEC's verticals are written against.
    """
    rows = await pg_pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    tables = {row["tablename"] for row in rows}
    assert {"workflow_executions", "history_events", "task_queue", "timers"} <= tables


# ---- the scaffold's own state ----------------------------------------------


async def test_start_workflow_is_still_a_todo(grpc_stub: rpc.WorkflowServiceAsyncStub) -> None:
    """The worklist, pinned. Delete this once V4's happy path lands.

    A `NotImplementedError` escaping a servicer surfaces to the client as
    UNKNOWN — gRPC's catch-all for "the handler raised something it did not map".
    """
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.StartWorkflow(
            pb.StartWorkflowRequest(
                workflow_type="order", workflow_id="o-1", task_queue="orders", input=b"{}"
            )
        )
    assert caught.value.code() == grpc.StatusCode.UNKNOWN
