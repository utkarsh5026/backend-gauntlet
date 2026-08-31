"""V3 — Retries with backoff + the dead-letter queue.

Jobs fail. The policy here is what keeps one bad job from taking the system down:
on failure, retry with an **exponentially backed-off, jittered** delay up to
`max_attempts`, then move the job to the **dead-letter queue** instead of looping
forever. A *poison message* (one that fails every time) is exactly the case the DLQ
exists for.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

import asyncpg

from . import metrics
from .job import Job, JobState

__all__ = ["BASE_DELAY", "MAX_DELAY", "Disposition", "RetryPolicy", "nack"]

BASE_DELAY: Final = 1.0
"""Seconds. The first retry waits at most this long."""

MAX_DELAY: Final = 300.0
"""Seconds. The cap the exponential curve pins at."""


class Disposition(StrEnum):
    """What happened to a job that failed."""

    RETRIED = "retried"
    """Rescheduled for another attempt after a backoff delay."""

    DEAD_LETTERED = "dead_lettered"
    """Out of attempts — moved to the dead-letter queue."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff parameters for the retry schedule."""

    base_delay: float = BASE_DELAY
    """Base unit of delay, in seconds; the first retry waits roughly this long."""

    max_delay: float = MAX_DELAY
    """Cap so the exponential curve can't schedule a job years out."""

    def ceiling(self, attempt: int) -> float:
        """The exponential **ceiling** for a given attempt: `base · 2^(attempt-1)`,
        capped at `max_delay`.

        This is the *upper bound* of the wait — the actual delay is a jittered draw
        within it (see :meth:`backoff`). Keeping it separate makes the growth curve
        deterministic and testable; the randomness lives only in `backoff`.

        The exponent is clamped at 0 so attempt 0 or 1 both mean "one base delay",
        and the whole expression is `min`-ed with the cap *before* returning, so a
        large attempt saturates instead of overflowing into an absurd float.
        """
        exponent = max(attempt - 1, 0)
        # Cap the exponent too: 2**exponent for a large attempt is a pointless
        # bignum whose only fate is to be min()-ed away.
        if exponent > 32:
            return self.max_delay
        return min(self.base_delay * (2**exponent), self.max_delay)

    def backoff(self, attempt: int) -> float:
        """**Full jitter** backoff (AWS's "Exponential Backoff And Jitter"): a
        uniformly random wait in `[0, ceiling(attempt)]`.

        The jitter is proportional to the *current* exponential ceiling, not to
        `max_delay` — so attempt 1 waits at most `base_delay`, while later attempts
        spread across the full cap. Because the ceiling is capped *before* the draw,
        the result can never exceed `max_delay`: no post-hoc clamp is needed.

        Allowing a draw of zero is deliberate. Fixed retries synchronise every
        failing worker into a thundering herd, and bare `2^n` still does — everyone
        retries at exactly the same instant. Spreading the draw across the whole
        interval, zero included, is what de-synchronises a herd that all failed at
        once instead of re-colliding it.
        """
        return random.uniform(0.0, self.ceiling(attempt))


async def nack(
    pool: asyncpg.Pool[asyncpg.Record],
    policy: RetryPolicy,
    job: Job,
    error: str,
) -> Disposition:
    """Handle a job that failed: bump the attempt, record the error, and either
    reschedule it with backoff or dead-letter it when its attempts are spent.

    The single `UPDATE` also clears the lease. That matters: a retried job must
    become claimable by *anyone*, and leaving `locked_by` set would let the reaper
    and the claim disagree about who owns a row that is now `ready`.

    The `attempts < max_attempts` comparison uses the *incremented* count, so a job
    with `max_attempts = 1` dead-letters on its first failure rather than getting a
    silent free retry.
    """
    attempts = job.attempts + 1
    delay = policy.backoff(attempts)
    run_at = datetime.now(UTC) + timedelta(seconds=delay)
    state = JobState.READY if attempts < job.max_attempts else JobState.DEAD

    await pool.execute(
        """
        UPDATE jobs
        SET
            attempts = $2,
            last_error = $3,
            state = $4,
            run_at = $5,
            locked_by = NULL,
            locked_at = NULL,
            locked_until = NULL
        WHERE id = $1
        """,
        job.id,
        attempts,
        error,
        state.value,
        run_at,
    )

    if state is JobState.READY:
        metrics.RETRIED_TOTAL.labels(kind=job.kind).inc()
        return Disposition.RETRIED

    metrics.DEAD_LETTERED_TOTAL.labels(kind=job.kind).inc()
    return Disposition.DEAD_LETTERED
