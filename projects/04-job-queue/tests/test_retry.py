"""V3 — retries with backoff and the dead-letter queue, against a real table.

The backoff *curve* is tested purely in `test_backoff`; this is about what `nack`
writes to the row and when a job stops being retried.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from job_queue.job import Job, JobState
from job_queue.queue import Queue
from job_queue.retry import Disposition, RetryPolicy, nack

from .conftest import LEASE, get_job, make_due_now, new_job

QUEUE = "emails"


async def claim_one(queue: Queue, max_attempts: int | None = None) -> Job:
    """Enqueue and claim a single job — the `running` row `nack` acts on."""
    await queue.enqueue(new_job(QUEUE, max_attempts=max_attempts))
    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert len(claimed) == 1
    return claimed[0]


async def test_nack_reschedules_with_remaining_attempts(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """A failure with budget left goes back to `ready`, with the attempt counted,
    the error recorded, the lease cleared, and `run_at` pushed into the future."""
    job = await claim_one(queue, max_attempts=3)
    before = datetime.now(UTC)

    assert await nack(pg_pool, RetryPolicy(), job, "smtp timeout") is Disposition.RETRIED

    updated = await get_job(queue, job.id)
    assert updated.state is JobState.READY
    assert updated.attempts == 1
    assert updated.last_error == "smtp timeout"
    assert updated.locked_until is None, "a retried job must be claimable by anyone"
    assert updated.run_at >= before, "backoff pushes run_at forward"

    assert await queue.claim(QUEUE, "w2", 10, LEASE) == [], "not claimable until due"
    await make_due_now(pg_pool, job.id)
    assert [j.id for j in await queue.claim(QUEUE, "w2", 10, LEASE)] == [job.id]


async def test_nack_dead_letters_when_attempts_exhausted(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """A one-shot job dead-letters on its first failure — the incremented count is
    what's compared, so `max_attempts = 1` gets no silent free retry."""
    job = await claim_one(queue, max_attempts=1)

    assert await nack(pg_pool, RetryPolicy(), job, "poison") is Disposition.DEAD_LETTERED

    updated = await get_job(queue, job.id)
    assert updated.state is JobState.DEAD
    assert updated.attempts == 1
    assert updated.last_error == "poison"
    assert [j.id for j in await queue.get_dlq(50, 0)] == [job.id]


async def test_nack_poison_message_reaches_dlq_and_stops(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """The case the DLQ exists for.

    A job that fails every time must land in the DLQ and **stop** — not loop
    forever. Driven the whole way through with a zero-delay policy so the loop runs
    at test speed rather than through real backoff.
    """
    max_attempts = 4
    instant = RetryPolicy(base_delay=0.0, max_delay=0.0)
    job_id = await queue.enqueue(new_job(QUEUE, max_attempts=max_attempts))

    dispositions: list[Disposition] = []
    for _ in range(max_attempts):
        claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
        if not claimed:
            break
        dispositions.append(await nack(pg_pool, instant, claimed[0], "poison"))
        await make_due_now(pg_pool, job_id)

    assert dispositions[-1] is Disposition.DEAD_LETTERED
    assert dispositions.count(Disposition.DEAD_LETTERED) == 1, "it dead-letters once, then stops"

    final = await get_job(queue, job_id)
    assert final.state is JobState.DEAD
    assert final.attempts == max_attempts
    assert await queue.claim(QUEUE, "w1", 10, LEASE) == [], "a dead job is never claimed again"
