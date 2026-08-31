"""V1 — The event-sourced history log, from scratch.

This is the part that makes a workflow *durable*. A workflow's state is not a row
you `UPDATE`; it is **derived** by replaying an append-only log of immutable
`Event`s (V2 does the folding — this module owns the log itself). Every fact the
engine knows about an execution — it started, an activity was scheduled, a timer
fired, it completed — is an event appended here, in order, forever.

Why event sourcing and not "a status column you mutate"? Because a mutable status
can only tell you *where you are*, never *how you got there* — and "how you got
there" is exactly what a fresh worker needs to resume a half-finished execution
after a crash. The log IS the state; the `workflow_executions.status` column is a
projection you may keep in sync, never a second source of truth.

The two invariants this module owns:

1. **Monotonic + gapless:** `event_id` is 1, 2, 3, … per run; an append that
   skips or reuses an id is a bug, and `(run_id, event_id)` being the primary key
   is what makes the database reject it rather than you remembering to.
2. **Append-only:** no code path updates or deletes a posted event.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .db import Executor
from .model import Event, RunId, WorkflowId

__all__ = ["HistoryStore", "StartOptions"]


@dataclass(frozen=True, slots=True)
class StartOptions:
    """Everything StartWorkflow needs to open a fresh history."""

    workflow_id: WorkflowId
    workflow_type: str
    task_queue: str
    input: bytes


class HistoryStore:
    """The durable event log, over `workflow_executions` + `history_events`.

    Every method takes an optional `conn`: pass the connection you already have a
    transaction open on and the write joins it, omit it and the pool runs it
    standalone (see `db.Executor`). That is what lets the dispatcher append events
    and schedule their side effects in *one* transaction — the atomicity V4 is
    graded on — without this module knowing anything about task queues.
    """

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def start_execution(
        self,
        opts: StartOptions,
        *,
        conn: Executor | None = None,
    ) -> RunId:
        """Open a new execution and append its very first event.

        TODO(V1): in ONE transaction, INSERT the `workflow_executions` row
        (status = `ExecutionStatus.RUNNING`) and INSERT its `WORKFLOW_STARTED`
        event as `event_id = 1`, carrying the input in `attributes`. Return the
        new run id — let Postgres mint it (`DEFAULT gen_random_uuid()` …
        `RETURNING run_id`) rather than generating one here, so there is one
        authority for it. The engine then schedules the first workflow task —
        that is the dispatcher's job, not this module's.

        Note the payload question you have to answer here and in
        `append_events`: `attributes` is JSONB, and JSON has no bytes type. Pick
        an encoding for an opaque payload (base64? a `bytea` column beside the
        JSON?) and apply it consistently, because whatever you choose, a replay
        two weeks from now has to decode exactly what a start wrote.
        """
        raise NotImplementedError("V1: create an execution and append WORKFLOW_STARTED as event 1")

    async def append_events(
        self,
        run_id: RunId,
        events: list[Event],
        *,
        conn: Executor | None = None,
    ) -> None:
        """Append `events` to a run's history **atomically and in order**.

        TODO(V1): INSERT every event for `run_id` in one transaction. The caller
        assigns the `event_id`s (from the replayed `WorkflowState.next_event_id`);
        your job is to make the whole batch land or none of it, so a partial
        append can never corrupt a history. `executemany` inside an explicit
        transaction is the shape — note that asyncpg's `executemany` is *already*
        atomic per call, but only when it is not competing with your own
        `execute` calls outside a transaction, which is precisely the case the
        dispatcher creates.

        A duplicate `event_id` must FAIL — the primary key enforces it, and that
        collision is how you would catch two workers trying to advance the same
        execution. Let `asyncpg.UniqueViolationError` reach you and translate it
        into something the caller can act on; do not `ON CONFLICT DO NOTHING`
        your way past it, which would silently drop a fact.
        """
        raise NotImplementedError("V1: atomically append an ordered batch of events")

    async def load_history(
        self,
        run_id: RunId,
        *,
        conn: Executor | None = None,
    ) -> list[Event]:
        """Load a run's **entire** history, ordered by `event_id`.

        What a non-sticky worker replays to rebuild state from scratch (V2).

        TODO(V1): `SELECT … FROM history_events WHERE run_id = $1 ORDER BY
        event_id`, and build an `Event` per row — `EventType(row["event_type"])`
        parses the column and raises `ValueError` on a value the enum does not
        know, which is a corrupt row and should not be quietly skipped.
        """
        raise NotImplementedError("V1: load a run's full ordered history")

    async def load_history_after(
        self,
        run_id: RunId,
        after_event_id: int,
        *,
        conn: Executor | None = None,
    ) -> list[Event]:
        """Load only the events with `event_id > after_event_id`.

        The delta a sticky worker (V5) needs to catch its cached state up without
        re-reading the whole log.

        TODO(V1): the same query as `load_history`, plus `AND event_id > $2`.
        Keep the two on one code path if you can — a suffix load that decodes
        rows differently from a full load is a bug that only shows up as "the
        sticky worker disagreed with the replay", which is the hardest kind of
        divergence to chase.
        """
        raise NotImplementedError("V1: load a run's history after an event id (the sticky delta)")


# TODO(V1): prove the log. These want a real Postgres — gate them on
# `DATABASE_URL` so the suite still passes without one (CI's python job runs with
# no database). Suggested cases:
#   - start_execution writes event 1 = WORKFLOW_STARTED and a 'running' row;
#   - append_events assigns 2, 3, 4… and load_history reads them back IN ORDER;
#   - appending a batch containing a duplicate event id FAILS and writes NOTHING
#     (the atomic-batch criterion — assert the history is unchanged afterwards);
#   - load_history_after(run, k) returns exactly the events with id > k;
#   - the status column agrees with the folded history at every step (V1's
#     "projection, not a second source of truth" criterion).
