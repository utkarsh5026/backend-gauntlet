"""Shared vocabulary for the workflow engine.

Every vertical speaks in these terms: the `Event`s that make up a history (V1),
the `WorkflowState` a replay folds them into (V2), the `Command`s a worker
returns, and the `TaskToken` that authorizes one response. These are
fully-implemented value types — the exception to "don't write the meat" — so the
interesting modules can stay about *behavior*, not vocabulary.

Two shapes are worth pausing on, because they are what "idiomatic Python" buys
you over a direct transcription of the Rust:

* `EventType` is a `StrEnum` whose members *are* the strings stored in the
  `history_events.event_type` column. There is no `as_db_str`/`from_db_str` pair
  to keep in sync: `str(EventType.TIMER_FIRED)` is already `"timer_fired"`, and
  `EventType(row["event_type"])` raises `ValueError` on an unknown value. One
  definition, both directions.
* `Command` is a **tagged union of frozen dataclasses**, not one struct with
  every field nullable. Each variant carries exactly the fields it needs, and
  `match cmd:` over them is checked: add a variant and every unhandled `match`
  becomes a type error rather than a silent fallthrough at 3am.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

__all__ = [
    "ActivityTask",
    "Command",
    "CompleteWorkflow",
    "Event",
    "EventType",
    "ExecutionStatus",
    "FailWorkflow",
    "PendingActivity",
    "RunId",
    "ScheduleActivity",
    "StartTimer",
    "TaskKind",
    "TaskToken",
    "WorkflowId",
    "WorkflowState",
    "WorkflowTask",
    "now_ms",
]

type WorkflowId = str
"""The caller's logical workflow id (chosen at StartWorkflow)."""

type RunId = UUID
"""Names one execution *attempt* of a workflow.

A UUID, so starting a workflow needs no coordinated sequence.
"""


class EventType(StrEnum):
    """The kind of a history event.

    A history is an append-only log of these; folding it left-to-right
    reconstructs the execution's state (V1/V2). The member *values* are what the
    `history_events.event_type` column stores, so the enum is the schema.
    """

    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_TASK_SCHEDULED = "workflow_task_scheduled"
    WORKFLOW_TASK_STARTED = "workflow_task_started"
    WORKFLOW_TASK_COMPLETED = "workflow_task_completed"
    ACTIVITY_SCHEDULED = "activity_scheduled"
    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_COMPLETED = "activity_completed"
    ACTIVITY_FAILED = "activity_failed"
    TIMER_STARTED = "timer_started"
    TIMER_FIRED = "timer_fired"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"

    @property
    def is_terminal(self) -> bool:
        """Is this the last event a history can ever have?"""
        return self in (EventType.WORKFLOW_COMPLETED, EventType.WORKFLOW_FAILED)


@dataclass(frozen=True, slots=True)
class Event:
    """One entry in an execution's history.

    `event_id` is monotonic per run and defines the replay order; `attributes`
    carries the event-type-specific payload (activity type + input, timer id +
    fire time, the workflow result, …) and is stored as JSONB.

    Frozen because the log is append-only: an event that could be mutated in
    memory is one an accidental `event.attributes["x"] = …` could desynchronise
    from the row it came from. A correction is a new event, never an edit.
    """

    event_id: int
    event_type: EventType
    timestamp_ms: int
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def new(
        cls,
        event_id: int,
        event_type: EventType,
        attributes: dict[str, Any] | None = None,
    ) -> Self:
        """Stamp a fresh event with the server's clock."""
        return cls(
            event_id=event_id,
            event_type=event_type,
            timestamp_ms=now_ms(),
            attributes=attributes or {},
        )


# ---- commands ----------------------------------------------------------------
#
# A decision the workflow made on its most recent task, decoded from the wire.
# The server validates the command stream against history (determinism, V2), then
# turns each command into events + the side effects it implies.


@dataclass(frozen=True, slots=True)
class ScheduleActivity:
    """Run an activity out-of-process on a worker."""

    activity_type: str
    input: bytes


@dataclass(frozen=True, slots=True)
class StartTimer:
    """Start a durable timer that fires `delay_ms` from now (V3)."""

    timer_id: str
    delay_ms: int


@dataclass(frozen=True, slots=True)
class CompleteWorkflow:
    """Finish the workflow successfully with this result."""

    result: bytes


@dataclass(frozen=True, slots=True)
class FailWorkflow:
    """Finish the workflow with a failure."""

    failure: str


type Command = ScheduleActivity | StartTimer | CompleteWorkflow | FailWorkflow
"""What a worker hands back after running one workflow task."""


class ExecutionStatus(StrEnum):
    """Where an execution is in its lifecycle.

    `RUNNING` is every non-terminal state; a replay sets `COMPLETED`/`FAILED`
    only when it folds the terminal event. The values double as the
    `workflow_executions.status` column's vocabulary.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PendingActivity:
    """A scheduled-but-unresolved activity.

    Keyed in `WorkflowState` by its `ACTIVITY_SCHEDULED` event id.
    """

    activity_type: str
    input: bytes


@dataclass(slots=True)
class WorkflowState:
    """The state produced by folding a history (V2).

    It is *derived*, never stored-and-mutated: two replays of the same events
    must yield an equal `WorkflowState` — which the dataclass `__eq__` below is
    there to let a test assert directly.

    Both maps are plain dicts. Python dicts are insertion-ordered, so a fold that
    inserts in event order iterates in event order; where you need an ordering
    that does *not* depend on insertion history (comparing two independently
    built states, rendering a stable digest) sort the keys explicitly rather than
    relying on it.
    """

    status: ExecutionStatus = ExecutionStatus.RUNNING
    next_event_id: int = 1
    """The id the *next* appended event will take (1 + the highest seen)."""

    pending_activities: dict[int, PendingActivity] = field(
        default_factory=dict[int, PendingActivity]
    )
    """Scheduled-but-unresolved activities, keyed by their schedule event id."""

    started_timers: dict[str, int] = field(default_factory=dict[str, int])
    """Started-but-unfired timers: workflow-assigned `timer_id` → fire epoch ms."""

    result: bytes | None = None
    """Set once the workflow completes."""

    failure: str | None = None
    """Set once the workflow fails."""

    @property
    def is_terminal(self) -> bool:
        return self.status is not ExecutionStatus.RUNNING


class TaskKind(StrEnum):
    """Whether a queued task drives the workflow function or an activity."""

    WORKFLOW = "workflow"
    ACTIVITY = "activity"


@dataclass(frozen=True, slots=True)
class TaskToken:
    """The opaque token a poll hands out and a respond hands back.

    It ties a response to exactly one dispatched task: the run, whether it is a
    workflow or activity task, and which scheduled event it corresponds to.
    Encoded as JSON bytes on the wire — the worker never inspects it, so the
    shape is ours to evolve.

    Opaque is not the same as trusted: `decode` proves only that the bytes are
    well-formed. Whether the sender still *holds* the task is a question only the
    live claim can answer (V4's stale-lease rule).
    """

    run_id: RunId
    kind: TaskKind
    scheduled_event_id: int

    def encode(self) -> bytes:
        """Serialize for the `task_token` wire field."""
        return json.dumps(
            {
                "run_id": str(self.run_id),
                "kind": str(self.kind),
                "scheduled_event_id": self.scheduled_event_id,
            }
        ).encode()

    @classmethod
    def decode(cls, raw: bytes) -> Self | None:
        """Parse a token off the wire.

        `None` if it is empty (a timed-out poll the worker echoed back) or
        malformed — the caller turns that into `INVALID_ARGUMENT`, never a 500.
        """
        if not raw:
            return None
        try:
            payload: Any = json.loads(raw)
            return cls(
                run_id=UUID(payload["run_id"]),
                kind=TaskKind(payload["kind"]),
                scheduled_event_id=int(payload["scheduled_event_id"]),
            )
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    """A workflow task handed to a worker.

    Carries the history to replay (V2) — or, on a sticky hit, only the events
    appended since this worker last ran the execution (V5) — plus the token to
    answer with.
    """

    token: TaskToken
    workflow_id: WorkflowId
    run_id: RunId
    history: list[Event]
    sticky_cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class ActivityTask:
    """An activity task handed to a worker: what to run, and with what input."""

    token: TaskToken
    workflow_id: WorkflowId
    run_id: RunId
    activity_type: str
    input: bytes


def now_ms() -> int:
    """Milliseconds since the Unix epoch — the engine's one clock for events.

    NOTE (V2): workflow *code* must never read the wall clock directly; that is
    exactly what makes a replay diverge. This helper is for the *server* stamping
    events and for timers. A workflow that needs "now" reads it from a recorded
    event, not from here.

    `time.time()`, not `time.monotonic()`: this value is persisted and compared
    across processes, and a monotonic clock's zero point is per-process. Use
    `time.monotonic()` for durations you measure inside one process (the sticky
    TTL does), and the wall clock for facts you write down.
    """
    return int(time.time() * 1000)
