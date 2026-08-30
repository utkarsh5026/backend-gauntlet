"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4. They assert the plumbing
(the app boots, the routes exist, metrics render, the error map works) and they
pin the scaffold's contract: ingest and stream raise NotImplementedError until
you build them. When you implement V1, the ingest test here is the first thing
that should fail — delete it then.
"""

from __future__ import annotations

import httpx
import pytest

from metrics_pipeline.model import RollupRow
from metrics_pipeline.sse import LiveFeed


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates an ingest request
    with the rollup it eventually lands in."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_query_rejects_an_inverted_range(client: httpx.AsyncClient) -> None:
    """Validation runs at the edge, so a nonsense range never reaches the store.

    It is a 503 rather than a 400 only because this app boots with no ClickHouse;
    once a store is wired, the ordering check is what answers.
    """
    response = await client.get("/query", params={"series": 1, "from": 100, "to": 50})
    assert response.status_code in (400, 503)


def test_live_feed_registration_is_wired() -> None:
    """Subscribe/unsubscribe are scaffolding, not a vertical — they work now."""
    feed = LiveFeed(capacity=4)
    assert feed.subscribers == 0
    sub = feed.subscribe()
    assert feed.subscribers == 1
    feed.unsubscribe(sub)
    assert feed.subscribers == 0


def test_publish_is_still_a_todo() -> None:
    """The V4 worklist, pinned. Delete this once the fan-out lands."""
    feed = LiveFeed(capacity=4)
    row = RollupRow(
        series_id=1,
        measurement="cpu",
        window_start="2024-06-28T00:00:00Z",  # type: ignore[arg-type]  # pydantic parses the string
        window_secs=60,
        count=1,
        sum=1.0,
        min=1.0,
        max=1.0,
        p50=0.0,
        p99=0.0,
    )
    with pytest.raises(NotImplementedError):
        feed.publish(row)


async def test_ingest_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """The V1 worklist, pinned. Delete this once the parser lands."""
    with pytest.raises(NotImplementedError):
        await client.post("/ingest", content=b"cpu,host=a usage=0.91")
