"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4. They assert the plumbing
(the app boots, the socket binds, membership sees itself, metrics render) and
they pin the scaffold's contract: a cache operation raises NotImplementedError
until you build it. When you implement V1, the last test here is the first thing
that should fail — delete it then.
"""

from __future__ import annotations

import httpx
import pytest


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_cluster_view_contains_self(client: httpx.AsyncClient) -> None:
    """Before any gossip, a node must still see itself as alive."""
    response = await client.get("/cluster")
    assert response.status_code == 200
    members = response.json()
    assert [m["node"]["id"] for m in members] == ["test-node"]
    assert members[0]["state"] == "alive"


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates a forwarded
    request with the node that served it.
    """
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_invalid_key_is_rejected_before_the_store(client: httpx.AsyncClient) -> None:
    """Validation runs at the edge, so a bad key never reaches a todo."""
    response = await client.get(f"/cache/{'k' * 600}")
    assert response.status_code == 400


async def test_cache_ops_are_still_a_todo(client: httpx.AsyncClient) -> None:
    """The scaffold's worklist, pinned. Delete this once V1 lands."""
    with pytest.raises(NotImplementedError):
        await client.get("/cache/hello")
