"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V6. They assert the plumbing
(both apps boot, the registry works, errors map correctly) and they pin the
scaffold's contract: an invocation raises NotImplementedError until you build it.
When you implement V1, the last tests here are the first things that should fail —
delete them then.
"""

from __future__ import annotations

import httpx
import pytest


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_create_and_list_functions(client: httpx.AsyncClient) -> None:
    """Registry plumbing must work from day one."""
    created = await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    assert created.status_code == 201
    assert created.json()["FunctionArn"].endswith(":function:hello")

    listed = await client.get("/2015-03-31/functions")
    functions = listed.json()["Functions"]
    assert [f["FunctionName"] for f in functions] == ["hello"]
    # Defaults from Settings are applied when the request omits them.
    assert functions[0]["MemorySize"] == 128


async def test_duplicate_function_is_a_conflict(client: httpx.AsyncClient) -> None:
    body = {"FunctionName": "hello", "Handler": "examples.hello.handler"}
    assert (await client.post("/2015-03-31/functions", json=body)).status_code == 201
    duplicate = await client.post("/2015-03-31/functions", json=body)
    assert duplicate.status_code == 409
    assert duplicate.headers["x-amzn-errortype"] == "ResourceConflictException"


async def test_unknown_function_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.post("/2015-03-31/functions/nope/invocations", json={})
    assert response.status_code == 404
    assert response.headers["x-amzn-errortype"] == "ResourceNotFoundException"


async def test_unknown_invocation_type_is_rejected(client: httpx.AsyncClient) -> None:
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    response = await client.post(
        "/2015-03-31/functions/hello/invocations",
        headers={"X-Amz-Invocation-Type": "Telepathy"},
        json={},
    )
    assert response.status_code == 400
    assert response.headers["x-amzn-errortype"] == "InvalidRequestContentException"


async def test_oversized_sync_payload_is_rejected(client: httpx.AsyncClient) -> None:
    """The size cap is enforced *before* execution, so it works on the scaffold."""
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    response = await client.post(
        "/2015-03-31/functions/hello/invocations",
        content=b"x" * (6_291_456 + 1),
    )
    assert response.status_code == 413
    assert response.headers["x-amzn-errortype"] == "RequestTooLargeException"


async def test_dry_run_does_not_execute(client: httpx.AsyncClient) -> None:
    """DryRun validates and stops — the one invocation type with nothing behind it."""
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    response = await client.post(
        "/2015-03-31/functions/hello/invocations",
        headers={"X-Amz-Invocation-Type": "DryRun"},
        json={},
    )
    assert response.status_code == 204


async def test_event_source_mapping_registration(client: httpx.AsyncClient) -> None:
    """Registering a mapping is plumbing; the poller behind it is V6."""
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    created = await client.post(
        "/2015-03-31/event-source-mappings",
        json={
            "FunctionName": "hello",
            "EventSourceArn": "http://localhost:8000",
            "BatchSize": 25,
        },
    )
    assert created.status_code == 202
    assert created.json()["BatchSize"] == 25

    listed = await client.get("/2015-03-31/event-source-mappings")
    assert len(listed.json()["EventSourceMappings"]) == 1


async def test_runtime_api_requires_an_environment_id(runtime_client: httpx.AsyncClient) -> None:
    """The header that identifies the polling environment is not optional.

    This is the check V3's "an environment cannot poll another's queue" is built
    on, so it holds even on the scaffold.
    """
    response = await runtime_client.get("/2018-06-01/runtime/invocation/next")
    assert response.status_code == 400
    assert response.headers["x-amzn-errortype"] == "InvalidRequestContentException"


async def test_sync_invoke_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """The scaffold's worklist, pinned. Delete this once V4 and V1 land."""
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    with pytest.raises(NotImplementedError):
        await client.post("/2015-03-31/functions/hello/invocations", json={"name": "world"})


async def test_async_invoke_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Same for the Event path — V5 owns it end to end."""
    await client.post(
        "/2015-03-31/functions",
        json={"FunctionName": "hello", "Handler": "examples.hello.handler"},
    )
    with pytest.raises(NotImplementedError):
        await client.post(
            "/2015-03-31/functions/hello/invocations",
            headers={"X-Amz-Invocation-Type": "Event"},
            json={"name": "world"},
        )


async def test_runtime_next_is_still_a_todo(runtime_client: httpx.AsyncClient) -> None:
    """And the runtime's side of the loop — V1."""
    with pytest.raises(NotImplementedError):
        await runtime_client.get(
            "/2018-06-01/runtime/invocation/next",
            headers={"Lambda-Runtime-Environment-Id": "env-1"},
        )
