"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V5. They assert the plumbing
(the app boots, the catalog registers tables, errors map correctly) and they pin
the scaffold's contract: a data-plane operation raises NotImplementedError until
you build it. When you implement V1, the last test here is the first thing that
should fail — delete it then.
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


async def test_create_and_list_tables(client: httpx.AsyncClient) -> None:
    """Catalog registration is plumbing and must work from day one."""
    created = await client.post(
        "/tables",
        json={"TableName": "orders", "PartitionKey": "pk", "SortKey": "sk"},
    )
    assert created.status_code == 201
    listed = await client.get("/tables")
    assert listed.json() == {"TableNames": ["orders"]}


async def test_duplicate_table_is_a_validation_error(client: httpx.AsyncClient) -> None:
    body = {"TableName": "orders", "PartitionKey": "pk"}
    assert (await client.post("/tables", json=body)).status_code == 201
    duplicate = await client.post("/tables", json=body)
    assert duplicate.status_code == 400
    assert duplicate.json()["__type"] == "ValidationException"


async def test_unknown_table_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/", headers={"X-Target": "GetItem"}, json={"TableName": "nope", "Key": {}}
    )
    assert response.status_code == 404
    assert response.json()["__type"] == "ResourceNotFoundException"


async def test_missing_target_header_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/", json={})
    assert response.status_code == 400


async def test_unknown_operation_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/", headers={"X-Target": "Frobnicate"}, json={})
    assert response.status_code == 400


async def test_data_plane_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """The scaffold's worklist, pinned. Delete this once V1 lands."""
    await client.post("/tables", json={"TableName": "orders", "PartitionKey": "pk"})
    with pytest.raises(NotImplementedError):
        await client.post(
            "/",
            headers={"X-Target": "PutItem"},
            json={"TableName": "orders", "Item": {"pk": {"S": "a"}}},
        )
