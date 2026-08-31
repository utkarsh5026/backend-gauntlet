"""V5 — Sticky workflow-state cache.

Replay (V2) is correct but not free: rebuilding a long-running workflow's state
means folding its *entire* history on every single task. A workflow with 10,000
events replays all 10,000 just to make its 10,001st decision. Temporal's fix is
**sticky execution**: after a worker runs a task, the engine routes that
execution's next task back to the *same* worker, which kept the folded
`WorkflowState` in memory. That worker then only needs the events since it last
ran — a handful, not the whole log.

The catch, and the lesson, is that this cache lives in a *specific worker's*
memory, so it is valid only while that worker is alive and reachable. This module
is the engine-side routing table: which execution is pinned to which worker, and
until when. If the sticky worker does not come back in time (it crashed — The
Reaper), the pin expires and the execution falls back to the normal queue, where
any worker picks it up with a **full replay**. Correctness never depends on the
cache; it only makes the common case cheap.

**No lock.** The Rust held a `Mutex<HashMap>`; this is a plain dict, and that is
not a shortcut. Every method here is fully synchronous — no `await` between
reading and writing — so on one event loop no other coroutine can interleave with
it, and a lock would guard against a race that cannot happen. The rule to
internalise (it is the async-Python rule, not a workflow-engine rule): a critical
section is safe without a lock exactly as long as it contains no `await`. Add one
— to log, to touch the database, to check the time over the network — and you
have reintroduced the race the lock was for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import RunId

__all__ = ["StickyCache", "StickyPin"]


@dataclass(frozen=True, slots=True)
class StickyPin:
    """A live pin.

    This execution's next workflow task should go to `worker_identity`, which
    cached the folded state through `last_event_id`, until `expires_at`.

    `expires_at` is a `time.monotonic()` deadline, not a wall-clock time: it is
    only ever compared against another reading from this same process, and the
    monotonic clock cannot jump backwards when NTP corrects the system time.
    Using the wall clock here means an NTP step can hand you a pin that outlives
    a dead worker — the exact failure the TTL exists to prevent.
    """

    worker_identity: str
    last_event_id: int
    expires_at: float


class StickyCache:
    """The engine-side sticky routing table.

    Process-local by design, never in Postgres: losing all of it costs replays,
    never correctness, and anything durable enough to survive a restart would be
    a second source of truth about a state that only one worker's memory actually
    holds.
    """

    def __init__(self, ttl: float) -> None:
        self._pins: dict[RunId, StickyPin] = {}
        self.ttl = ttl
        """The stickiness window in seconds — how long a pin survives without
        the worker polling before the execution falls back to the normal queue."""

    def lookup(self, run_id: RunId) -> StickyPin | None:
        """Return this execution's live pin, if it has one.

        Used at poll time to decide "sticky delta" vs "full replay".

        TODO(V5): look up `run_id` and return the pin only if it has not expired
        (`time.monotonic()` against `pin.expires_at`). An expired pin means the
        worker went silent — treat it as gone and drop it here, so the table does
        not accumulate pins to workers that died an hour ago. A hit lets the
        caller ship just the events after `last_event_id`; a miss means a
        full-history workflow task on the normal queue.
        """
        raise NotImplementedError("V5: return a live (non-expired) pin for this execution, if any")

    def pin(self, run_id: RunId, worker_identity: str, last_event_id: int) -> None:
        """Pin (or refresh) an execution to the worker that just ran it.

        Records how far that worker's cached state now extends. Called on
        RespondWorkflowTaskCompleted.

        TODO(V5): insert/replace the pin for `run_id` with `expires_at =
        time.monotonic() + self.ttl`. This is what makes the NEXT task
        sticky-eligible for `worker_identity`. Last writer wins: if a different
        worker completed this execution, the pin follows the state, because that
        is where the cached state now is.
        """
        raise NotImplementedError("V5: pin an execution to the worker that cached its state")

    def evict(self, run_id: RunId) -> None:
        """Drop an execution's pin.

        The worker was lost, or the execution finished. The next poll for it is a
        normal-queue full replay.

        TODO(V5): remove `run_id` from the table if present — and note that
        `dict.pop(key, None)` is the idiomatic "remove if there", not a `if key
        in …` check followed by a `del`, which is two lookups and one more line
        to get wrong.
        """
        raise NotImplementedError("V5: drop a pin so the execution falls back to full replay")

    def __len__(self) -> int:
        """How many pins are currently held — including any not yet reaped.

        Worth watching. Pins are only dropped when something looks them up or
        evicts them, so an execution that is pinned and then never polled again
        (its workflow completed, say) leaves its entry behind. On a scaffold that
        is nothing; at the boss fight's throughput it is a slow leak, and where
        you put the reaping — on lookup, on completion, on a sweep — is a design
        decision worth making on purpose rather than discovering.
        """
        return len(self._pins)


# TODO(V5): this is pure in-memory logic — test it with NO database:
#   - pin then lookup within the TTL is a hit carrying the right last_event_id;
#   - lookup after the TTL elapses is a miss (and drops the stale pin — assert
#     through `len(cache)`, and fake the clock rather than sleeping: build the
#     cache with a tiny TTL, or monkeypatch `time.monotonic`);
#   - evict makes a subsequent lookup a miss, and evicting an unknown run is a
#     no-op, not a KeyError;
#   - pinning the same run to a new worker re-routes it (last writer wins).
