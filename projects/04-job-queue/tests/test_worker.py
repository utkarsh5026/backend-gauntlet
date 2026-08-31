"""The worker loop end to end: claim -> run -> ack/nack, and the V4 wakeup.

These drive a *real* spawned worker against a real table, which is the only way to
catch the wiring bugs the per-vertical tests can't see — a claim that never
dispatches, an ack that never lands, an idle worker that never wakes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from job_queue.job import JobId, JobState
from job_queue.queue import Queue
from job_queue.retry import RetryPolicy
from job_queue.worker import WorkerConfig, run

from .conftest import get_job, new_job, unique_queue, wait_until


def spawn_worker(
    queue: Queue,
    queue_name: str,
    poll_interval: float,
    log_dir: Path,
    *,
    max_attempts_policy: RetryPolicy | None = None,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """Start one worker draining `queue_name`. Returns its shutdown flag and task."""
    shutdown = asyncio.Event()
    cfg = WorkerConfig(
        queue_name=queue_name,
        poll_interval=poll_interval,
        visibility_timeout=30.0,
        claim_batch=10,
        retry=max_attempts_policy or RetryPolicy(base_delay=0.0, max_delay=0.0),
        log_dir=log_dir,
    )
    return shutdown, asyncio.create_task(run("w0", queue, cfg, shutdown))


async def stop(shutdown: asyncio.Event, task: asyncio.Task[None]) -> None:
    shutdown.set()
    try:
        async with asyncio.timeout(5):
            await task
    except TimeoutError:  # pragma: no cover - a worker that won't stop is a failure
        task.cancel()
        raise AssertionError("worker did not stop within 5s of the shutdown flag") from None


async def reaches(queue: Queue, job_id: JobId, state: JobState, within: float) -> float | None:
    async def done() -> bool:
        job = await queue.get(job_id)
        return job is not None and job.state is state

    return await wait_until(done, within)


async def test_ready_job_is_drained_by_a_running_worker(queue: Queue, tmp_path: Path) -> None:
    """The control case: an immediately-ready job drains promptly.

    Proves the harness (spawn -> enqueue -> run -> done) works, so a failure of the
    delayed-job test below is about the wakeup gap, not the scaffolding.
    """
    queue_name = unique_queue("now")
    shutdown, worker = spawn_worker(queue, queue_name, 0.1, tmp_path)
    try:
        job_id = await queue.enqueue(new_job(queue_name, kind="noop", payload=None))
        assert await reaches(queue, job_id, JobState.DONE, 5.0) is not None
    finally:
        await stop(shutdown, worker)


async def test_delayed_job_coming_due_is_not_stranded_by_a_long_poll(
    queue: Queue, tmp_path: Path
) -> None:
    """An idle worker wakes when a delayed job *comes due*.

    `enqueue` NOTIFYs at insert time — too early, the job isn't claimable yet — and
    nothing NOTIFYs at t+1s when its `run_at` arrives. With a 10s poll fallback, a
    worker that only woke on notify-or-poll would strand this job for ~10 seconds.
    It runs in ~1s only because the idle sleep is bounded by the earliest ready
    job's `run_at`.
    """
    queue_name = unique_queue("due")
    shutdown, worker = spawn_worker(queue, queue_name, 10.0, tmp_path)
    try:
        job_id = await queue.enqueue(new_job(queue_name, kind="noop", payload=None, delay_secs=1))
        took = await reaches(queue, job_id, JobState.DONE, 5.0)
        assert took is not None, (
            "a job delayed 1s never ran within 5s — nothing wakes an idle worker when a "
            "delayed job comes due, so it stranded behind the 10s poll fallback"
        )
    finally:
        await stop(shutdown, worker)


async def test_failing_job_is_retried_then_dead_lettered(queue: Queue, tmp_path: Path) -> None:
    """V3 through the worker: a poison job burns its attempts and lands in the DLQ.

    A zero-delay retry policy keeps this at test speed; the *curve* is asserted in
    `test_backoff`. What matters here is that the worker drives the job to a
    terminal state instead of hot-looping on it.
    """
    queue_name = unique_queue("poison")
    shutdown, worker = spawn_worker(queue, queue_name, 0.05, tmp_path)
    try:
        job_id = await queue.enqueue(new_job(queue_name, kind="fail", payload=None, max_attempts=3))
        assert await reaches(queue, job_id, JobState.DEAD, 10.0) is not None

        job = await get_job(queue, job_id)
        assert job.attempts == 3, "it used its whole budget before dying"
        assert job.last_error == "poison"
        assert [j.id for j in await queue.get_dlq(50, 0)] == [job_id]
    finally:
        await stop(shutdown, worker)


async def test_flaky_job_succeeds_on_a_later_attempt(queue: Queue, tmp_path: Path) -> None:
    """The other half of retry: a job that fails a few times and then works ends
    `done`, not dead — the retry path has to be able to *recover*, not just give up.

    `fail_n=2` means "fail while `attempts <= 2`", so it fails on attempts 0, 1 and
    2 and succeeds on the fourth run, leaving `attempts` at 3.
    """
    queue_name = unique_queue("flaky")
    shutdown, worker = spawn_worker(queue, queue_name, 0.05, tmp_path)
    try:
        job_id = await queue.enqueue(
            new_job(queue_name, kind="flaky_then_ok", payload={"fail_n": 2}, max_attempts=5)
        )
        assert await reaches(queue, job_id, JobState.DONE, 10.0) is not None
        assert (await get_job(queue, job_id)).attempts == 3, "three failures, then success"
    finally:
        await stop(shutdown, worker)


async def test_worker_stops_promptly_on_shutdown(queue: Queue, tmp_path: Path) -> None:
    """A worker parked on a long poll still exits quickly when told to.

    This is the graceful-shutdown property: `stop` fails the test if the worker
    takes longer than 5s, and a worker that only checked the flag between polls
    would take the full 30s poll interval.
    """
    queue_name = unique_queue("stop")
    shutdown, worker = spawn_worker(queue, queue_name, 30.0, tmp_path)
    await asyncio.sleep(0.2)  # let it reach the idle park
    await stop(shutdown, worker)
    assert worker.done()


async def test_unroutable_kind_is_dead_lettered_not_crashed(queue: Queue, tmp_path: Path) -> None:
    """An unknown `kind` must fail the *job*, never the worker.

    The queue treats `kind` as nothing but a routing key; a value with no registered
    handler is a bad enqueue, and it should end in the DLQ with a readable
    `last_error` while the worker carries on draining.
    """
    queue_name = unique_queue("unroutable")
    shutdown, worker = spawn_worker(queue, queue_name, 0.05, tmp_path)
    try:
        bad_id = await queue.enqueue(
            new_job(queue_name, kind="no_such_handler", payload={}, max_attempts=1)
        )
        assert await reaches(queue, bad_id, JobState.DEAD, 10.0) is not None
        last_error = (await get_job(queue, bad_id)).last_error
        assert last_error is not None and "unroutable" in last_error

        # The worker is still alive and still draining.
        good_id = await queue.enqueue(new_job(queue_name, kind="noop", payload=None))
        assert await reaches(queue, good_id, JobState.DONE, 5.0) is not None
    finally:
        await stop(shutdown, worker)
