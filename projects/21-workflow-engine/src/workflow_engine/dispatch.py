"""V4 — Worker dispatch: task queues, long-poll, at-least-once delivery.

Workers do not get *pushed* work; they **long-poll** for it. A worker calls
`PollWorkflowTask` and the call blocks until either a task is claimable or the
poll times out (return empty, poll again). This is how the engine matches idle
workers to ready work without a scheduler that has to know who is alive.

The delivery guarantee is **at-least-once**, deliberately: when a worker claims a
task it takes a *visibility-timeout lease*, not ownership. Complete the task in
time and the lease is released (the row is deleted); crash before you do — The
Reaper — and the lease lapses, the task becomes claimable again, and another
worker replays the history and carries on. At-least-once + deterministic replay
(V2) + idempotent effects is what adds up to "durable execution".

This module is also the server-side **orchestrator**: it turns the commands a
worker returns into history events and their side effects (schedule an activity
task, start a durable timer via `TimerService`, complete the execution), and
refreshes the sticky pin (V5). It leans on `HistoryStore` (V1), `replay` (V2),
`TimerService` (V3) and `StickyCache` (V5) — it is the glue that makes them one
engine.

**The long-poll, in async Python.** A poll that finds nothing has to wait without
burning anything. The lazy version — `while ...: await asyncio.sleep(0.05)` then
re-query — costs one query per poller per 50ms, so 200 idle workers are 4,000
queries/sec against a database with nothing to say. Two better shapes, and the
SPEC grades that you chose between them on purpose:

* an `asyncio.Event` per (queue, kind) that whoever enqueues sets, with
  `asyncio.timeout(...)` bounding the wait. Cheap and exact — but in-process
  only, so a second engine instance's enqueue does not wake this one's pollers.
* Postgres `LISTEN`/`NOTIFY` (`conn.add_listener`), which crosses processes at
  the cost of a dedicated connection and a delivery guarantee that is
  best-effort — so the poll fallback stays, as a backstop rather than the
  mechanism.

Whichever you pick, a parked poller must also wake on `self.shutdown`, or a
`docker stop` waits out the full long-poll timeout on every idle worker.
"""

from __future__ import annotations

import asyncio

import asyncpg
import structlog

from .config import Settings
from .history import HistoryStore, StartOptions
from .model import ActivityTask, Command, RunId, TaskToken, WorkflowState, WorkflowTask
from .sticky import StickyCache
from .timers import TimerService

__all__ = ["Dispatcher"]

log = structlog.get_logger(__name__)


class Dispatcher:
    """The task-queue engine + server-side orchestrator.

    One instance backs every RPC and the timer scan loop; there is nothing
    per-worker in it.
    """

    def __init__(
        self,
        pool: asyncpg.Pool[asyncpg.Record],
        history: HistoryStore,
        timers: TimerService,
        sticky: StickyCache,
        settings: Settings,
    ) -> None:
        self.pool = pool
        self.history = history
        self.timers = timers
        self.sticky = sticky
        self.settings = settings

        self.shutdown = asyncio.Event()
        """Set by `main` on SIGTERM.

        Parked long-polls must wait on this alongside their timeout and return
        "no work" when it fires: a task claimed and then abandoned by our own
        shutdown is the one lost-work case the engine inflicts on itself, and it
        is the checklist item that says so.
        """

    async def start_workflow(self, opts: StartOptions) -> RunId:
        """Start a new execution and enqueue its first workflow task.

        TODO(V4): in one transaction, create the execution + `WORKFLOW_STARTED`
        via `self.history.start_execution(opts, conn=conn)`, then INSERT a
        `kind = 'workflow'` row into `task_queue` for `opts.task_queue` so a
        worker can pick it up. Return the run id.

        One transaction, not two calls: an execution whose first task was never
        enqueued is a workflow that exists and will never run — and nothing in
        the system will ever notice, because it is not late, it is just absent.
        """
        raise NotImplementedError("V4: open a history and enqueue the first workflow task")

    async def poll_workflow_task(self, task_queue: str, identity: str) -> WorkflowTask | None:
        """Long-poll for the next workflow task. `None` means the poll timed out.

        TODO(V4/V5): claim a pending `kind = 'workflow'` task — `FOR UPDATE SKIP
        LOCKED`, ordered by `visible_at` — setting `visible_at = now() +
        visibility_timeout` and `locked_by = identity`. Then build the
        `WorkflowTask`:

          - sticky HIT: `self.sticky.lookup(run_id)` pins this run to `identity`
            → ship only the events after the pin's `last_event_id`
            (`HistoryStore.load_history_after`), `sticky_cache_hit=True`;
          - sticky MISS: ship the full history (`HistoryStore.load_history`) so
            the worker replays from scratch.

        Record `metrics.REPLAYS_TOTAL.labels(sticky=…)` and
        `metrics.EVENTS_REPLAYED.labels(sticky=…).observe(len(history))` — the
        second is what proves a hit is *cheaper*, not merely more frequent. Block
        up to `self.settings.long_poll_timeout` when nothing is claimable, then
        return `None`.

        Watch the claim's transaction boundary: the row lock and the `UPDATE`
        that leases the task have to commit before you go off to load history, or
        every poller serializes behind one long-lived transaction. Claim, commit,
        *then* read the history for the task you now own.
        """
        raise NotImplementedError("V4/V5: long-poll + claim a workflow task, sticky-aware")

    async def complete_workflow_task(self, token: TaskToken, commands: list[Command]) -> None:
        """Apply the commands a worker produced for one workflow task.

        This is the orchestrator's core, and the SPEC grades that it is one
        transaction.

        TODO(V2/V4/V5): in a single transaction:

          1. Resolve the live claim for `token` and reject a **stale** one — a
             worker whose lease already lapsed and whose task was reassigned
             cannot commit its result on top of the new owner's. Note that the
             worker's identity comes from the claim row (`locked_by`), not from
             the request: an identity the caller asserts about itself is not
             evidence, and trusting it would let any worker inherit another's
             sticky pin.
          2. Validate the worker did not diverge from history
             (`replay.check_determinism`) → `NonDeterministic`, and count
             `metrics.NONDETERMINISM_TOTAL`.
          3. Turn each command into event(s) and the side effect it implies —
             `match cmd:` over the `Command` union gives you one arm each:
               ScheduleActivity → append ACTIVITY_SCHEDULED + enqueue an activity task
               StartTimer       → append TIMER_STARTED + `self.timers.schedule_timer(conn, …)`
               CompleteWorkflow → append WORKFLOW_COMPLETED, mark the execution
                                  terminal, `metrics.EXECUTIONS_COMPLETED_TOTAL`
               FailWorkflow     → append WORKFLOW_FAILED, likewise
          4. Append WORKFLOW_TASK_COMPLETED, delete the claimed task row, and
             refresh the sticky pin (`StickyCache.pin`) so this worker's next
             task is sticky — evicting it instead when the execution just went
             terminal.

        Every step takes the same `conn`, which is what makes "no event without
        its side effect, no side effect without its event" true rather than
        aspirational. And note the one step that is *not* in the transaction and
        cannot be: the sticky pin is in this process's memory, so it commits when
        the dict is written, not when Postgres does. Decide what happens if the
        transaction then rolls back.
        """
        raise NotImplementedError("V4: apply a worker's commands to history + their side effects")

    async def poll_activity_task(self, task_queue: str, identity: str) -> ActivityTask | None:
        """Long-poll for the next activity task. `None` means the poll timed out.

        TODO(V4): claim a pending `kind = 'activity'` task (`SKIP LOCKED`), lease
        it with the visibility timeout, and read its `activity_type` + input from
        the `ACTIVITY_SCHEDULED` event it points at — the task row is a *pointer*
        into history, not a copy of the work, so there is exactly one place the
        input lives. Count `metrics.ACTIVITY_TASKS_TOTAL`.
        """
        raise NotImplementedError("V4: long-poll + claim an activity task")

    async def complete_activity_task(self, token: TaskToken, result: bytes) -> None:
        """Record an activity's successful result and wake its workflow.

        TODO(V4): in one transaction, append ACTIVITY_COMPLETED (with `result`)
        for the scheduled event the token names, delete the activity task row,
        and enqueue a workflow task so the workflow can react. Reject a token
        whose claim is no longer live, for the same reason as above.
        """
        raise NotImplementedError("V4: record ACTIVITY_COMPLETED + the follow-up workflow task")

    async def fail_activity_task(self, token: TaskToken, failure: str) -> None:
        """Record an activity failure and wake its workflow.

        TODO(V4): append ACTIVITY_FAILED (with `failure`), delete the task row,
        enqueue a workflow task — the workflow decides whether to retry or handle
        it, which is the difference between an activity and a workflow task. A
        server-side retry policy on top of this is a natural stretch.
        """
        raise NotImplementedError("V4: record ACTIVITY_FAILED + the follow-up workflow task")

    async def get_result(self, run_id: RunId) -> WorkflowState:
        """The execution's current state — terminal result when done, else running.

        TODO(V4): load the history (`HistoryStore.load_history`) and fold it
        (`replay.replay`); raise `errors.NotFound` if the run id is unknown.
        Deriving the answer rather than reading the `status` column is the point:
        it is the same code path a resuming worker takes, so a bug here is a bug
        there, and it cannot silently disagree with the log.
        """
        raise NotImplementedError("V4: load + replay a run to report its current state")


# TODO(V4): the proofs the SPEC asks for. These need a real Postgres — and, more
# than that, they need *separate sessions*: `FOR UPDATE SKIP LOCKED` does nothing
# within one connection, so a test that shares a connection between its two
# "workers" proves nothing. Suggested cases:
#   - two concurrent pollers on one queue with one pending task: exactly one gets
#     it, the other gets None;
#   - a claimed-and-abandoned task is redelivered after the visibility timeout,
#     and the workflow still completes exactly once;
#   - a completion with a token whose lease already lapsed is refused, and the
#     new owner's result is the one that lands;
#   - a poll with no work returns None after ~the long-poll timeout, and returns
#     promptly (not after the full timeout) once `shutdown` is set.
