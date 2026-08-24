"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V5. They assert the plumbing
(the app boots, the shards are created, stats and metrics render, validation runs
at the edge) and they pin the scaffold's contract: indexing and searching raise
`NotImplementedError` until you build them. When you implement V1, the last two
tests here are the first things that should fail — delete them then.
"""

from __future__ import annotations

import httpx
import pytest

from full_text_search.shard import ShardedIndex

# Pinned outputs of the blake2b router at SHARD_COUNT=2. Stable across
# processes and machines — that is the property being asserted.
ROUTE_DOC_1 = 1
ROUTE_DOC_2 = 0


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_stats_reports_empty_shards(client: httpx.AsyncClient) -> None:
    """Stats is fully wired, so it must report honestly on a bare index."""
    response = await client.get("/_stats")
    assert response.status_code == 200
    body = response.json()
    assert body["shard_count"] == 2
    assert body["total_docs"] == 0
    assert body["total_segments"] == 0
    assert [s["shard"] for s in body["shards"]] == [0, 1]


async def test_refresh_and_forcemerge_are_clean_noops(client: httpx.AsyncClient) -> None:
    """Nothing buffered and nothing to merge must not reach a vertical's TODO —
    this is what makes the admin routes usable on the bare scaffold."""
    assert (await client.post("/_refresh")).json() == {"refreshed": 0}
    assert (await client.post("/_forcemerge")).json() == {"merged_segments": 0}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    # The series exist at zero before any event — a counter that only appears
    # after the first request is one you cannot alert on.
    assert "search_documents_indexed_total" in response.text
    assert "search_segments" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates a client's trace
    with the log lines the engine emitted while serving it."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_empty_document_is_rejected_at_the_edge(client: httpx.AsyncClient) -> None:
    """Validation runs before the engine, so a bad document never reaches a TODO."""
    response = await client.post("/documents", json={"text": "   "})
    assert response.status_code == 400


async def test_search_size_is_bounded(client: httpx.AsyncClient) -> None:
    """The SPEC's bounded-result-set rule: a client cannot ask for the corpus."""
    response = await client.get("/search", params={"q": "rust", "size": 100_000})
    assert response.status_code == 422


def test_routing_is_stable_across_processes(engine: ShardedIndex) -> None:
    """The one piece of V5 that is wired, and the one most easily broken.

    Hard-coded expectations on purpose: these values must not change when the
    interpreter restarts. Swap the digest for the builtin `hash()` and this test
    fails on *some* runs and passes on others — which is the worst possible
    failure mode, so it is pinned to fail deterministically instead.
    """
    assert engine.route("doc-1") == ROUTE_DOC_1
    assert engine.route("doc-2") == ROUTE_DOC_2
    # Keyless documents spread rather than pile onto one shard.
    assert {engine.route(None) for _ in range(4)} == {0, 1}


async def test_indexing_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """The scaffold's worklist, pinned. Delete this once V1 lands."""
    with pytest.raises(NotImplementedError):
        await client.post("/documents", json={"text": "hello world"})


async def test_search_is_still_a_todo(client: httpx.AsyncClient) -> None:
    """Search hits V1's analyzer before it ever reaches the fan-out."""
    with pytest.raises(NotImplementedError):
        await client.get("/search", params={"q": "hello"})
