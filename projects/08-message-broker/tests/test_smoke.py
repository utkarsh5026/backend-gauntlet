"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4. They assert the plumbing
(the app boots, the data dir is laid out, topic admin works, validation runs at
the edge, metrics render) and they pin the scaffold's contract: produce and fetch
raise until you build them. When you implement V1 and V3, the last two tests here
are the first things that should fail — delete them then.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from message_broker.config import Settings


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates a producer's
    request with the append the broker logged for it."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_create_and_list_topic(client: httpx.AsyncClient, settings: Settings) -> None:
    """Topic admin is plumbing, so it works end to end already — including the
    directory tree it lays down, since the filesystem *is* the broker."""
    created = await client.post("/topics", json={"name": "orders", "partitions": 4})
    assert created.status_code == 201
    assert created.json() == {"name": "orders", "partitions": 4}

    listed = await client.get("/topics")
    assert listed.json() == {"topics": [{"name": "orders", "partitions": 4}]}

    partition_dirs = sorted(p.name for p in (settings.data_dir / "topics" / "orders").iterdir())
    assert partition_dirs == ["0", "1", "2", "3"]


async def test_creating_a_topic_twice_conflicts(client: httpx.AsyncClient) -> None:
    await client.post("/topics", json={"name": "orders"})
    again = await client.post("/topics", json={"name": "orders"})
    assert again.status_code == 409


@pytest.mark.parametrize("name", ["../escape", "with/slash", "..", "", "sp ace"])
async def test_illegal_topic_names_are_rejected(client: httpx.AsyncClient, name: str) -> None:
    """The name becomes a directory. Validation runs before anything touches the
    filesystem — the security-horizontal item."""
    response = await client.post("/topics", json={"name": name})
    assert response.status_code == 400


async def test_default_partition_count_is_used(client: httpx.AsyncClient) -> None:
    response = await client.post("/topics", json={"name": "events"})
    assert response.json()["partitions"] == 3


async def test_produce_to_unknown_topic_is_404(client: httpx.AsyncClient) -> None:
    """Resolved before the produce path, so an unknown topic never reaches a
    todo."""
    response = await client.post("/topics/nope/records", json={"records": [{"value": "hello"}]})
    assert response.status_code == 404


async def test_oversized_record_is_rejected_before_the_log(client: httpx.AsyncClient) -> None:
    """The size cap is enforced at the edge, so one client cannot stream the
    broker out of disk."""
    await client.post("/topics", json={"name": "orders"})
    response = await client.post(
        "/topics/orders/records", json={"records": [{"value": "x" * 70_000}]}
    )
    assert response.status_code == 413


async def test_fetch_bounds_the_batch(client: httpx.AsyncClient) -> None:
    """A fetch can never be asked to return the whole log — rejected by the
    query-parameter bound, not by the log."""
    await client.post("/topics", json={"name": "orders"})
    response = await client.get(
        "/topics/orders/partitions/0/records", params={"max_records": 10_000}
    )
    assert response.status_code == 422


async def test_fetch_from_unknown_partition_is_404(client: httpx.AsyncClient) -> None:
    await client.post("/topics", json={"name": "orders", "partitions": 2})
    response = await client.get("/topics/orders/partitions/7/records")
    assert response.status_code == 404


async def test_produce_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """The scaffold's worklist, pinned. Delete once V3 + V1 land."""
    await client.post("/topics", json={"name": "orders"})
    with pytest.raises(NotImplementedError):
        await client.post("/topics/orders/records", json={"records": [{"value": "hi"}]})


async def test_fetch_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Ditto — delete once V1's read path lands."""
    await client.post("/topics", json={"name": "orders"})
    with pytest.raises(NotImplementedError):
        await client.get("/topics/orders/partitions/0/records")


def test_data_dir_is_created_on_open(tmp_path: Path) -> None:
    """Sanity on the layout itself: `topics/` and `groups/` exist before any
    request, because recovery has to have somewhere to look."""
    from message_broker.broker import Broker

    settings = Settings(data_dir=tmp_path / "data")
    Broker.open(settings.data_dir, settings.log_config, settings.default_partitions)
    assert (settings.data_dir / "topics").is_dir()
    assert (settings.data_dir / "groups").is_dir()
