"""V1 — the claim engine: enqueue, the `SKIP LOCKED` dequeue, ack, and the DLQ views."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg

from job_queue.job import JobId, JobState
from job_queue.queue import Queue
from job_queue.retry import Disposition, RetryPolicy, nack

from .conftest import LEASE, get_job, new_job, row

QUEUE = "emails"


# ---- enqueue ---------------------------------------------------------------


async def test_enqueue_inserts_ready_job_and_returns_id(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """A fresh enqueue lands a `ready`, un-attempted row that round-trips the
    queue/kind/payload verbatim and returns a positive BIGSERIAL id."""
    job_id = await queue.enqueue(new_job(QUEUE))
    assert job_id > 0

    record = await row(pg_pool, job_id)
    assert record["queue"] == QUEUE
    assert record["kind"] == "noop"
    assert record["payload"] == {"to": "a@b.com"}
    assert record["state"] == "ready", "a new job starts ready"
    assert record["attempts"] == 0, "no attempts have run yet"


async def test_enqueue_uses_explicit_max_attempts_over_default(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    job_id = await queue.enqueue(new_job(QUEUE, max_attempts=2))
    assert (await row(pg_pool, job_id))["max_attempts"] == 2


async def test_enqueue_falls_back_to_default_max_attempts(
    pg_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    queue = Queue(pg_pool, 7)
    job_id = await queue.enqueue(new_job(QUEUE))
    assert (await row(pg_pool, job_id))["max_attempts"] == 7


async def test_enqueue_with_delay_sets_future_run_at(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """`delay_secs` pushes `run_at` into the future, so the job isn't claimable
    until it's due (V4's delayed delivery)."""
    delay = 60
    before = datetime.now(UTC)
    job_id = await queue.enqueue(new_job(QUEUE, delay_secs=delay))
    after = datetime.now(UTC)

    run_at = (await row(pg_pool, job_id))["run_at"]
    # Postgres TIMESTAMPTZ has microsecond resolution, so the stored value is
    # truncated below Python's precision — slacken the lower bound by a hair.
    assert before + timedelta(seconds=delay, milliseconds=-1) <= run_at
    assert run_at <= after + timedelta(seconds=delay)


async def test_enqueue_clamps_negative_delay_to_now(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """A job can be scheduled into the future but never into the past."""
    before = datetime.now(UTC)
    job_id = await queue.enqueue(new_job(QUEUE, delay_secs=-100))
    after = datetime.now(UTC)

    run_at = (await row(pg_pool, job_id))["run_at"]
    assert before - timedelta(milliseconds=1) <= run_at <= after


# ---- claim -----------------------------------------------------------------


async def test_claim_returns_ready_job_once_then_empty(queue: Queue) -> None:
    """A claimed job comes back exactly once: flipped to `running`, stamped with a
    future lease, and — no longer `ready` — invisible to a second claim."""
    job_id = await queue.enqueue(new_job(QUEUE))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert [j.id for j in claimed] == [job_id]
    assert claimed[0].state is JobState.RUNNING
    locked_until = claimed[0].locked_until
    assert locked_until is not None and locked_until > datetime.now(UTC)

    assert await queue.claim(QUEUE, "w2", 10, LEASE) == []


async def test_claim_respects_limit_batch(queue: Queue) -> None:
    """A backlog is drained in batches: 5 ready jobs at limit=2 come out 2, 2, 1."""
    for _ in range(5):
        await queue.enqueue(new_job(QUEUE))

    sizes = [len(await queue.claim(QUEUE, "w1", 2, LEASE)) for _ in range(4)]
    assert sizes == [2, 2, 1, 0]


async def test_claim_skips_jobs_not_yet_due(queue: Queue) -> None:
    """A job whose `run_at` is in the future is invisible to `claim` until due."""
    due_id = await queue.enqueue(new_job(QUEUE))
    await queue.enqueue(new_job(QUEUE, delay_secs=300))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert [j.id for j in claimed] == [due_id]


async def test_claim_is_scoped_to_its_queue(queue: Queue) -> None:
    """`claim` never hands out another queue's job, even when both are ready."""
    mine = await queue.enqueue(new_job(QUEUE))
    await queue.enqueue(new_job("other"))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert [j.id for j in claimed] == [mine]


async def test_claim_returns_oldest_first(queue: Queue) -> None:
    """Ordering is `run_at` ascending, so a backlog drains FIFO rather than LIFO."""
    first = await queue.enqueue(new_job(QUEUE, delay_secs=-30))
    second = await queue.enqueue(new_job(QUEUE, delay_secs=-20))
    third = await queue.enqueue(new_job(QUEUE, delay_secs=-10))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert [j.id for j in claimed] == [first, second, third]


async def test_concurrent_claimers_never_double_dispatch(
    queue: Queue,
) -> None:
    """The `SKIP LOCKED` guarantee under contention.

    N workers racing over a backlog of M jobs claim M **distinct** jobs in total —
    no job is ever handed to two workers. Each worker drains in batches until it
    sees an empty claim; because every claim flips its rows to `running` in one
    committed statement, the backlog strictly shrinks and all workers terminate.

    This is the test that fails if `claim` is ever split back into a `SELECT` and a
    separate `UPDATE`.
    """
    backlog = 60
    workers = 6
    for _ in range(backlog):
        await queue.enqueue(new_job(QUEUE))

    async def drain(worker_id: str) -> list[JobId]:
        seen: list[JobId] = []
        while True:
            claimed = await queue.claim(QUEUE, worker_id, 5, LEASE)
            if not claimed:
                return seen
            seen.extend(job.id for job in claimed)

    results = await asyncio.gather(*(drain(f"w{n}") for n in range(workers)))
    claimed_ids = [job_id for result in results for job_id in result]

    assert len(claimed_ids) == backlog, "every job was claimed exactly once"
    assert len(set(claimed_ids)) == backlog, "a job was handed to two workers"


# ---- ack / get -------------------------------------------------------------


async def test_ack_marks_job_done_and_unclaimable(queue: Queue) -> None:
    job_id = await queue.enqueue(new_job(QUEUE))
    await queue.claim(QUEUE, "w1", 10, LEASE)
    await queue.ack(job_id)

    job = await get_job(queue, job_id)
    assert job.state is JobState.DONE
    assert job.locked_until is None, "ack releases the lease"
    assert await queue.claim(QUEUE, "w2", 10, LEASE) == []


async def test_get_returns_job_or_none(queue: Queue) -> None:
    job_id = await queue.enqueue(new_job(QUEUE))
    found = await queue.get(job_id)
    assert found is not None and found.id == job_id
    assert await queue.get(job_id + 10_000) is None, "an unknown id is a 404, not an error"


# ---- the DLQ views ---------------------------------------------------------


async def dead_letter_one(queue: Queue, queue_name: str) -> JobId:
    """Drive one job all the way to `dead`: enqueue it one-shot, claim it, nack it."""
    job_id = await queue.enqueue(new_job(queue_name, max_attempts=1))
    claimed = await queue.claim(queue_name, "w1", 10, LEASE)
    target = next(job for job in claimed if job.id == job_id)
    disposition = await nack(queue.pool, RetryPolicy(), target, "poison")
    assert disposition is Disposition.DEAD_LETTERED
    return job_id


async def test_requeue_revives_a_dead_job_with_a_fresh_budget(queue: Queue) -> None:
    """The V3.4 headline: a dead job is inspectable in the DLQ and requeueable back
    to a fresh, claimable life."""
    job_id = await dead_letter_one(queue, QUEUE)
    assert [j.id for j in await queue.get_dlq(50, 0)] == [job_id]

    revived = await queue.requeue(job_id)
    assert revived is not None
    assert revived.state is JobState.READY
    assert revived.attempts == 0, "requeue resets the attempt budget"

    assert await queue.get_dlq(50, 0) == [], "it left the DLQ"
    assert [j.id for j in await queue.claim(QUEUE, "w2", 10, LEASE)] == [job_id]


async def test_requeue_ignores_non_dead_and_unknown_jobs(queue: Queue) -> None:
    """The `state = 'dead'` guard, which is what stops the admin door resurrecting a
    live job into a concurrent double-run."""
    ready_id = await queue.enqueue(new_job(QUEUE))
    assert await queue.requeue(ready_id) is None, "a ready job is not requeueable"

    running = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert await queue.requeue(running[0].id) is None, "a leased job is not requeueable"

    assert await queue.requeue(999_999) is None, "an unknown id is not requeueable"


async def test_get_dlq_paginates_newest_first(queue: Queue) -> None:
    """`id DESC` paging: `limit` caps the page and `offset` walks down without
    overlap. (The route layer additionally *clamps* the caller's limit — that
    boundary lives in `routes`, not here, which trusts its args.)"""
    dead = [await dead_letter_one(queue, QUEUE) for _ in range(5)]
    newest_first = list(reversed(dead))

    assert [j.id for j in await queue.get_dlq(2, 0)] == newest_first[:2]
    assert [j.id for j in await queue.get_dlq(2, 2)] == newest_first[2:4]
    assert [j.id for j in await queue.get_dlq(2, 4)] == newest_first[4:]
