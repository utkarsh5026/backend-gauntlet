"""V2 — the visibility timeout: leases, the reaper, and at-least-once delivery."""

from __future__ import annotations

import asyncpg

from job_queue.job import JobState
from job_queue.lease import reap_expired
from job_queue.queue import Queue

from .conftest import LEASE, expire_lease, get_job, new_job

QUEUE = "emails"


async def test_reap_requeues_expired_lease_and_frees_it_for_reclaim(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """The core at-least-once guarantee.

    A worker claims a job then "dies" (never acks); once its lease lapses the reaper
    returns the job to `ready`, clears the stale lease, and a *second* worker
    reclaims the same job. Without this sweep, a crashed worker's job is stuck
    `running` forever — neither done nor available to anyone.
    """
    job_id = await queue.enqueue(new_job(QUEUE))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert claimed[0].state is JobState.RUNNING
    await expire_lease(pg_pool, job_id)  # w1 crashes; the lease lapses

    assert await reap_expired(pg_pool) == 1

    job = await get_job(queue, job_id)
    assert job.state is JobState.READY, "the reaper returns it to ready"
    assert job.locked_until is None, "the reaper clears the stale lease"

    reclaimed = await queue.claim(QUEUE, "w2", 10, LEASE)
    assert [j.id for j in reclaimed] == [job_id], "a second worker reclaims it"


async def test_reap_leaves_live_lease_untouched(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """A lease still in the future is left strictly alone.

    The reaper must not be a "requeue anything running" sweep — that would yank
    jobs out from under workers that are simply still working.
    """
    job_id = await queue.enqueue(new_job(QUEUE))
    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    lease = claimed[0].locked_until

    assert await reap_expired(pg_pool) == 0

    job = await get_job(queue, job_id)
    assert job.state is JobState.RUNNING, "still held by w1"
    assert job.locked_until == lease, "the lease is unchanged"
    assert await queue.claim(QUEUE, "w2", 10, LEASE) == [], "a live-leased job is not claimable"


async def test_reap_ignores_non_running_jobs_even_with_stale_lease(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """The `state = 'running'` guard: the reaper filters on state, not just on the
    clock. A `done` job carrying a stale `locked_until` must never be resurrected
    into a second execution."""
    done_id = await queue.enqueue(new_job(QUEUE))
    await queue.claim(QUEUE, "w1", 10, LEASE)
    await queue.ack(done_id)
    await expire_lease(pg_pool, done_id)

    ready_id = await queue.enqueue(new_job(QUEUE))

    assert await reap_expired(pg_pool) == 0

    assert (await get_job(queue, done_id)).state is JobState.DONE
    assert (await get_job(queue, ready_id)).state is JobState.READY


async def test_reap_returns_count_of_all_expired_leases(
    queue: Queue, pg_pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """One sweep reaps every expired lease, not just the first."""
    for _ in range(3):
        await queue.enqueue(new_job(QUEUE))

    claimed = await queue.claim(QUEUE, "w1", 10, LEASE)
    assert len(claimed) == 3
    for job in claimed:
        await expire_lease(pg_pool, job.id)

    assert await reap_expired(pg_pool) == 3
