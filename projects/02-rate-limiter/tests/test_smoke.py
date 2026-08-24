"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V3. They assert the
plumbing (the server binds, reflection and health answer, validation runs at the
edge, metrics render) and they pin the scaffold's contract: a real decision
raises until you build it. When you implement V3, the last test here is the
first thing that should fail — delete it then.
"""

from __future__ import annotations

import grpc
import grpc.aio
import httpx
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from rate_limiter.limiter import Decision
from rate_limiter.pb import ratelimit_pb2 as pb
from rate_limiter.pb import ratelimit_pb2_grpc as rpc
from rate_limiter.service import to_response


async def test_healthz(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_metrics_endpoint_renders(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_grpc_health_reports_serving(grpc_channel: grpc.aio.Channel) -> None:
    """`grpc.health.v1` is what a gRPC load balancer checks — it must answer."""
    stub = health_pb2_grpc.HealthStub(grpc_channel)
    response = await stub.Check(health_pb2.HealthCheckRequest(service=""))
    assert response.status == health_pb2.HealthCheckResponse.SERVING


async def test_empty_key_is_invalid_argument(grpc_stub: rpc.RateLimiterAsyncStub) -> None:
    """Validation runs before the backend, so garbage never costs a round-trip."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.Check(pb.CheckRequest(key="", cost=1))
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_absurd_cost_is_invalid_argument(grpc_stub: rpc.RateLimiterAsyncStub) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.Check(pb.CheckRequest(key="user-1", cost=10_000))
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_decision_maps_onto_the_wire() -> None:
    """`retry_after` is seconds internally and milliseconds on the wire."""
    response = to_response(Decision.deny(retry_after=1.5, limit=20))
    assert response.allowed is False
    assert response.remaining == 0
    assert response.limit == 20
    assert response.retry_after_ms == 1500


async def test_check_is_still_a_todo(grpc_stub: rpc.RateLimiterAsyncStub) -> None:
    """The scaffold's worklist, pinned. Delete this once V3 lands.

    A `NotImplementedError` escaping a servicer surfaces to the client as
    UNKNOWN — gRPC's catch-all for "the handler raised something it did not map".
    """
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await grpc_stub.Check(pb.CheckRequest(key="user-1", cost=1))
    assert caught.value.code() == grpc.StatusCode.UNKNOWN
