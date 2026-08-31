"""V3 — Durable timers.

A workflow that says "wait 3 days, then charge the card" cannot hold that delay
in an `asyncio.sleep` — the process will not live three days, and if it dies the
sleep dies with it. There is no version of this that works in memory: a task
parked on `await asyncio.sleep(259_200)` is a Python object on one event loop,
and a deploy is enough to lose it.

A durable timer is a **persisted due-time**: the `START_TIMER` command writes a
row *in the same transaction* that appends `TIMER_STARTED`, so the timer can
never be lost with the process that created it, and a background scanner fires it
later by appending `TIMER_FIRED` and scheduling the wake-up. Restart the whole
engine mid-wait and the timer still fires — because it was never in memory to
begin with.

The invariant this module owns: a timer fires **exactly once** into history. The
scanner may run repeatedly, may overlap a restart, and may be running on two
engine instances at once, so firing has to be idempotent — `(run_id, timer_id)`
is the key, and `TIMER_FIRED` lands at most once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncpg
import structlog

from .db import Executor
from .model import RunId

if TYPE_CHECKING:
    # Imported for typing only: `dispatch` imports this module for TimerService,
    # so importing it back at runtime would be a cycle. The annotations in this
    # file are strings (`from __future__ import annotations`), so the name only
    # has to exist for the type checker.
    from .dispatch import Dispatcher

__all__ = ["DueTimer", "TimerService", "fire_due_timers", "scan_loop"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DueTimer:
    """A pending timer the scan found due, ready to fire."""

    run_id: RunId
    timer_id: str
    started_event_id: int


class TimerService:
    """The durable timer store, backed by the `timers` table."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def schedule_timer(
        self,
        conn: Executor,
        run_id: RunId,
        timer_id: str,
        started_event_id: int,
        delay_ms: int,
    ) -> None:
        """Record a durable timer.

        `conn` is first and required on purpose: this call **must** run inside the
        transaction that appends the `TIMER_STARTED` event, so the timer and its
        event commit together — never one without the other. A signature that let
        you omit it would let you write the atomicity bug by accident, and the
        symptom (a timer no history explains, or an event no scanner will ever
        fire) shows up days later in production.

        TODO(V3): INSERT into `timers` (run_id, timer_id, started_event_id,
        fire_at, state='pending') on `conn`. Compute `fire_at` **in the
        database** — `now() + $4::interval` with a `datetime.timedelta`
        parameter, which asyncpg maps to `interval` — not from a Python clock:
        the scan compares against `now()` on the server, and two machines'
        wall clocks disagreeing is not a bug you want to debug through a timer
        that fired early.
        """
        raise NotImplementedError("V3: persist a durable timer alongside its TIMER_STARTED event")

    async def claim_due(self, conn: Executor, limit: int) -> list[DueTimer]:
        """Claim up to `limit` timers whose `fire_at` has passed.

        TODO(V3): `SELECT … FROM timers WHERE state = 'pending' AND fire_at <=
        now() ORDER BY fire_at FOR UPDATE SKIP LOCKED LIMIT $1`, returned as
        `DueTimer`s. `SKIP LOCKED` is what stops two engine instances firing the
        same timer: the second one steps over the rows the first has locked
        instead of blocking behind them.

        The Python-specific trap is `conn`, again, and it is a silent one: row
        locks live until the **transaction** ends, and asyncpg runs a bare
        `pool.fetch(...)` in its own implicit transaction that commits the moment
        the call returns. Claim through the pool and your locks are released
        before you have fired anything — `SKIP LOCKED` will appear to work in a
        single-instance test and double-fire the day you run two. The claim and
        the fire have to share one `async with conn.transaction():`.
        """
        raise NotImplementedError("V3: claim due timers with SKIP LOCKED")

    async def mark_fired(self, conn: Executor, run_id: RunId, timer_id: str) -> None:
        """Mark a timer fired so the next scan skips it (idempotent completion).

        TODO(V3): `UPDATE timers SET state = 'fired' WHERE run_id = $1 AND
        timer_id = $2`, on the same connection — and therefore in the same
        transaction — that appends `TIMER_FIRED` and schedules the wake-up, so a
        crash mid-fire does all three or none.
        """
        raise NotImplementedError("V3: mark a timer fired atomically with its TIMER_FIRED event")


async def fire_due_timers(
    timers: TimerService,
    dispatcher: Dispatcher,
    batch: int,
) -> int:
    """Fire every currently-due timer. Returns how many fired.

    TODO(V3): open one transaction; claim due timers (`TimerService.claim_due`);
    for each, append a `TIMER_FIRED` event through the history store, enqueue a
    workflow task on the execution's task queue through the dispatcher, and
    `TimerService.mark_fired` — all on that same connection, so a mid-fire crash
    leaves the timer still `pending` to retry, never a `TIMER_FIRED` with no
    wake-up. Increment `metrics.TIMERS_FIRED_TOTAL` per fire.

    Two shapes to weigh, and the SPEC grades the reasoning either way: one
    transaction for the whole batch is simplest and makes a partial batch
    impossible, but holds locks for as long as the slowest fire; a transaction
    per timer releases sooner at the cost of a longer claim loop. Whichever you
    pick, do not be tempted into firing the batch concurrently with
    `asyncio.gather` over connections from the pool — the claim's locks belong to
    one transaction, and a second connection cannot see them.
    """
    raise NotImplementedError("V3: fire due timers durably — TIMER_FIRED + wake-up, exactly once")


async def scan_loop(
    timers: TimerService,
    dispatcher: Dispatcher,
    interval: float,
    batch: int,
    shutdown: asyncio.Event,
) -> None:
    """The background scan loop. Wiring — complete, and not part of the worklist.

    Started from `main` only when `RUN_TIMER_SERVICE=true`, so the bare scaffold
    serves without this raising on its first pass. It wakes every `interval`
    seconds, fires whatever is due, and returns when `shutdown` is set.

    The shutdown shape is the graded part: this checks the event *between* passes
    and never mid-pass, so SIGTERM lets the current scan finish rather than
    abandoning half-fired timers. `main` awaits this task, which is what turns
    "the loop noticed" into "the process waited".
    """
    log.info("durable timer scan loop started", interval_seconds=interval, batch=batch)
    while not shutdown.is_set():
        try:
            # asyncio.timeout, not asyncio.sleep: this both paces the loop and
            # makes shutdown immediate. A plain sleep would leave the process
            # hanging for up to `interval` on every SIGTERM.
            async with asyncio.timeout(interval):
                await shutdown.wait()
            break  # the event fired; drain.
        except TimeoutError:
            pass  # the interval elapsed: time for a pass.

        try:
            fired = await fire_due_timers(timers, dispatcher, batch)
            if fired:
                log.debug("timers fired", count=fired)
        except NotImplementedError:
            # The scaffold's own state. Stop rather than raise this every
            # `interval` — one honest line beats five per second of the same
            # worklist item.
            log.warning("timer scan is a V3 worklist item; scan loop stopping")
            return
        except Exception:
            # A failed scan must not kill the loop: the timers are still durable
            # and still due, so the next pass retries them. This is the whole
            # reason at-least-once firing has to be idempotent.
            log.exception("timer scan failed")

    log.info("timer scan loop drained")
