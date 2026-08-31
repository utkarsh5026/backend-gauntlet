"""Graceful shutdown: the bounded final flush of the click ingestor.

Kept out of `main.py` (wiring only) so the timeout policy - and the tests that
pin it - live next to the logic they cover.

There is no signal handling here, and that is not an omission. Uvicorn installs
the SIGTERM/SIGINT handlers, stops accepting connections, lets in-flight requests
finish, and *then* runs the lifespan's shutdown half. So "drain on SIGTERM" is
implemented by putting this in the lifespan's `finally` - the same place the
pool and the Redis client are closed.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Final

import structlog

from .ingest import ClickIngestor

__all__ = ["SHUTDOWN_FLUSH_BUDGET", "ShutdownOutcome", "drain_ingestor"]

log = structlog.get_logger(__name__)

SHUTDOWN_FLUSH_BUDGET: Final[float] = 5.0
"""Seconds to wait for the click buffer to flush before giving up.

Comfortably under a typical orchestrator's SIGTERM->SIGKILL grace period (30s on
Kubernetes) so we exit on our own terms rather than being killed mid-write."""


class ShutdownOutcome(Enum):
    """How the final flush went."""

    FLUSHED = "flushed"
    """Buffer written and the ingestor task returned within budget."""

    FAILED = "failed"
    """The ingestor task raised while flushing (the batch may be lost)."""

    TIMED_OUT = "timed_out"
    """The flush did not finish within budget; we exit without it."""


async def drain_ingestor(
    ingestor: ClickIngestor,
    task: asyncio.Task[None],
    budget: float = SHUTDOWN_FLUSH_BUDGET,
) -> ShutdownOutcome:
    """Stop the ingestor and wait for its final flush, bounded by `budget`.

    The budget is the accepted trade-off: a wedged database write must not hold
    the process past the SIGKILL deadline, so we would rather drop one last
    batch of analytics than hang and be killed mid-flush.
    """
    try:
        async with asyncio.timeout(budget):
            await ingestor.stop()
            await task
    except TimeoutError:
        log.warning(
            "ingestor flush exceeded shutdown budget; exiting with clicks possibly unflushed",
            budget_secs=budget,
        )
        task.cancel()
        return ShutdownOutcome.TIMED_OUT
    except Exception as exc:  # noqa: BLE001 - a broken flush must not break shutdown
        log.warning("ingestor task failed during shutdown flush", error=str(exc))
        return ShutdownOutcome.FAILED

    log.info("click buffer flushed, ingestor stopped cleanly")
    return ShutdownOutcome.FLUSHED
