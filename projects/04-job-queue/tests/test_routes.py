"""The producer + admin HTTP API, driven over ASGI.

Covers the SPEC's API criteria — the status codes, the auth gate on the mutating
routes, and the caps the route layer owns (the DLQ page clamp, the body limit).
"""

from __future__ import annotations

import asyncpg
import httpx

from job_queue.job import JobState
from job_queue.queue import Queue
from job_queue.retry import Disposition, RetryPolicy, nack
from job_queue.routes import MAX_BODY_BYTES, MAX_DLQ_LIMIT

from .conftest import LEASE, new_job

QUEUE = "emails"


# ---- liveness --------------------------------------------------------------


async def test_healthz_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200


async def test_metrics_endpoint_renders_the_registry(client: httpx.AsyncClient) -> None:
    """`/metrics` is what makes the observability checklist checkable."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "job_queue_enqueued_total" in response.text


# ---- enqueue ---------------------------------------------------------------


async def test_enqueue_returns_201_and_an_id(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/jobs",
        json={"queue": QUEUE, "kind": "noop", "payload": {"to": "a@b.com"}},
        headers=auth,
    )
    assert response.status_code == 201
    assert response.json()["id"] > 0


async def test_enqueue_requires_a_bearer_token(client: httpx.AsyncClient) -> None:
    """An open `POST /jobs` is not merely "make my workers busy" — with the
    `exec`/`shell` kinds it is remote code execution on every worker."""
    body = {"queue": QUEUE, "kind": "noop"}
    assert (await client.post("/jobs", json=body)).status_code == 401
    wrong = {"authorization": "Bearer nope"}
    assert (await client.post("/jobs", json=body, headers=wrong)).status_code == 401


async def test_requeue_requires_a_bearer_token(client: httpx.AsyncClient) -> None:
    assert (await client.post("/job/1/requeue")).status_code == 401


async def test_malformed_body_is_400_not_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The SPEC asks for 400 on a malformed body, and a caller should not have to
    parse two different error envelopes depending on which layer rejected them."""
    response = await client.post(
        "/jobs", json={"queue": "bad queue!", "kind": "noop"}, headers=auth
    )
    assert response.status_code == 400
    assert "error" in response.json()


async def test_oversized_payload_is_rejected(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/jobs",
        json={"kind": "noop", "payload": {"blob": "x" * 70_000}},
        headers=auth,
    )
    assert response.status_code == 400


async def test_oversized_body_is_refused_before_parsing(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The coarse outer guard fires on `content-length`, so an enormous body is
    refused without being read and parsed first."""
    response = await client.post(
        "/jobs",
        content=b"x" * (MAX_BODY_BYTES + 1024),
        headers={**auth, "content-type": "application/json"},
    )
    assert response.status_code == 413


# ---- reads -----------------------------------------------------------------


async def test_get_job_round_trips_then_404s(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await client.post("/jobs", json={"queue": QUEUE, "kind": "noop"}, headers=auth)
    job_id = created.json()["id"]

    found = await client.get(f"/jobs/{job_id}")
    assert found.status_code == 200
    body = found.json()
    assert body["id"] == job_id
    assert body["state"] == JobState.READY.value

    assert (await client.get(f"/jobs/{job_id + 10_000}")).status_code == 404


async def test_stats_reports_depth_by_state(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    for _ in range(3):
        await client.post("/jobs", json={"queue": "default", "kind": "noop"}, headers=auth)
    body = (await client.get("/stats")).json()
    assert body["counts"]["ready"] == 3


# ---- the DLQ ---------------------------------------------------------------


async def dead_letter_one(pool: asyncpg.Pool[asyncpg.Record]) -> int:
    queue = Queue(pool, 5)
    job_id = await queue.enqueue(new_job(QUEUE, max_attempts=1))
    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    target = next(job for job in claimed if job.id == job_id)
    assert await nack(pool, RetryPolicy(), target, "poison") is Disposition.DEAD_LETTERED
    return job_id


async def test_dlq_lists_dead_jobs(
    client: httpx.AsyncClient, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    job_id = await dead_letter_one(pg_pool)
    body = (await client.get("/dlq")).json()
    assert [job["id"] for job in body] == [job_id]
    assert body[0]["last_error"] == "poison"


async def test_dlq_limit_is_clamped_not_rejected(
    client: httpx.AsyncClient, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """An admin page asking for 10,000 rows gets the biggest page allowed, not a
    400 — but it can never pull an unbounded DLQ into one response."""
    for _ in range(3):
        await dead_letter_one(pg_pool)

    assert (await client.get("/dlq", params={"limit": 100_000})).status_code == 200
    assert len((await client.get("/dlq", params={"limit": 2})).json()) == 2
    assert MAX_DLQ_LIMIT == 200


async def test_requeue_revives_then_404s_on_a_live_job(
    client: httpx.AsyncClient,
    pg_pool: asyncpg.Pool[asyncpg.Record],
    auth: dict[str, str],
) -> None:
    job_id = await dead_letter_one(pg_pool)

    revived = await client.post(f"/job/{job_id}/requeue", headers=auth)
    assert revived.status_code == 200
    assert revived.json()["state"] == JobState.READY.value

    # Now that it is ready again, the dead-only guard turns the second call into 404.
    assert (await client.post(f"/job/{job_id}/requeue", headers=auth)).status_code == 404
