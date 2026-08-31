"""V2 — Deterministic replay: fold a history into state.

A workflow's state is not stored; it is **recomputed** by folding its history
left-to-right. `replay` is that fold — a *pure function* from `list[Event]` to
`WorkflowState`. "Pure" is the entire point: replay the same events on any
worker, at any time, and you must get an identical state. That determinism is
what lets a crashed execution resume on a different machine as if nothing
happened.

It is also why workflow *code* has rules (the worker enforces them, but the
engine has to understand them): a workflow may not read the wall clock, generate
a random number, or hit the network directly — every such effect would produce a
different result on replay. Instead the workflow issues `Command`s and the
effects come back as *recorded events*, which replay deterministically.

**Python's own determinism traps**, since the language will happily hand you a
fold that is only accidentally reproducible:

* `set` iteration order depends on hash values, and `str`/`bytes` hashes are
  randomized per process (`PYTHONHASHSEED`). A fold that iterates a set is
  non-deterministic *across processes* — which is exactly the axis that matters
  here, and exactly the one a single-process test cannot see.
* `dict` is insertion-ordered, so it is safe as long as you never rely on an
  order that came from anywhere but the events themselves.
* No `time.time()`, no `random`, no `os.environ`, no IO. `replay` should be
  callable with the network unplugged and give the same answer next year.

The engine's stake in all this is `check_determinism`: when a worker reports the
commands its replayed code produced, the engine confirms they are consistent with
the history the worker was told to replay. A worker whose code changed under a
running execution emits commands that do not match — and it gets caught here
rather than silently corrupting the execution.
"""

from __future__ import annotations

from .model import Command, Event, WorkflowState

__all__ = ["check_determinism", "replay"]


def replay(history: list[Event]) -> WorkflowState:
    """Fold a full history into the current `WorkflowState`.

    Pure and deterministic: no clock, no IO, no randomness — only the events
    decide the result. Raises `errors.CorruptHistory` on a history that cannot be
    folded honestly.

    TODO(V2): start from a fresh `WorkflowState()` and apply each event in
    `event_id` order. Each event type moves the state:

      - WORKFLOW_STARTED          → running; stash the input if you need it.
      - ACTIVITY_SCHEDULED        → add a `PendingActivity` keyed by the event id.
      - ACTIVITY_COMPLETED/FAILED → remove the matching pending activity.
      - TIMER_STARTED             → `started_timers[timer_id] = fire_at`.
      - TIMER_FIRED               → remove it.
      - WORKFLOW_COMPLETED        → status = COMPLETED, set `result`.
      - WORKFLOW_FAILED           → status = FAILED, set `failure`.
      - (the workflow-task scheduled/started/completed events advance
        bookkeeping only — but decide what "bookkeeping" means and be consistent,
        because `next_event_id` is derived from every event, not just the
        interesting ones.)

    Keep `next_event_id` = 1 + the highest `event_id` seen. Reject an
    out-of-order or duplicate id, and reject an event that resolves an activity
    or timer that was never scheduled — a gap means a corrupt history, not
    something to paper over. `match event.event_type:` over the enum gives you
    one arm per case and a place to put the "unknown event type" arm that should
    never run.

    A note on the return type: this hands back a *mutable* `WorkflowState` that
    the fold built up. That is fine — the purity that matters is that the same
    input yields an equal output, not that the object is frozen — but do not hand
    the same instance to two callers and let one of them mutate it.
    """
    raise NotImplementedError("V2: fold the event history into a WorkflowState, deterministically")


def check_determinism(
    history: list[Event],
    replayed_through: int,
    commands: list[Command],
) -> None:
    """Confirm a worker's `commands` are consistent with the history it replayed.

    When a worker finishes a workflow task it hands back the commands its
    (replayed) code produced. If this execution has been advanced before, history
    already *records* what those commands must be — so a replay that produces
    different commands is non-determinism, and this is where the engine catches
    it. Returns `None` when the commands are consistent; raises
    `errors.NonDeterministic` when they are not.

    TODO(V2): reconstruct the state at `replayed_through` (fold `history` up to
    that event id), then check `commands` against what the recorded events after
    that point imply — a `ScheduleActivity` should line up with the recorded
    `ACTIVITY_SCHEDULED`, a `StartTimer` with the recorded `TIMER_STARTED`, and
    so on, in order. `zip(commands, recorded, strict=False)` walks the overlap;
    the length mismatch at the end is its own kind of divergence and deserves its
    own message.

    On a mismatch raise `NonDeterministic` naming the *first* divergence
    ("expected schedule_activity(charge), got schedule_activity(refund)") — that
    message is a workflow author's best debugging clue, and it is the one error
    string in this engine that goes to the caller verbatim. On a first-ever task
    (no later events yet) there is nothing to contradict, so any commands are
    accepted and become the record.

    Structural equality does the comparison for you: `Command` is a union of
    frozen dataclasses, so `recorded_command == returned_command` compares
    variant *and* fields, and a `match` over the pair is where you turn a
    mismatch into a message that names which field differed.
    """
    raise NotImplementedError("V2: detect a worker whose replay diverged from recorded history")


# TODO(V2): replay is a pure function — test it with NO database, just lists of
# events. Suggested cases:
#   - replay([]) == WorkflowState();
#   - a start → schedule-activity → activity-completed → complete history folds
#     to a COMPLETED state with the right result and no pending activities;
#   - a hypothesis property test over generated valid histories:
#       replay(h) == replay(h)                              (idempotent)
#       replay(h) == fold(replay(h[:k]), h[k:])             (split-invariant)
#     Generating *valid* histories is most of the work — write a strategy that
#     builds them by construction (a small state machine emitting legal next
#     events) rather than filtering random ones, or hypothesis will spend its
#     budget rejecting.
#   - a history with a gap, an out-of-order id, or an ACTIVITY_COMPLETED for an
#     activity that was never scheduled raises CorruptHistory;
#   - check_determinism accepts a first-ever task's commands, and raises
#     NonDeterministic naming the first divergence when they contradict history.
