"""V5 — Asynchronous invocation: the queue nobody sees until it retries twice.

An `Event`-type invoke is acknowledged in milliseconds and then owned entirely by
the platform. What happens next is a delivery contract your callers depend on
without ever having read it:

    accepted -> queued -> attempt 1 -> (fail) -> backoff -> attempt 2
             -> (fail) -> backoff -> attempt 3 -> (fail) -> dead-letter

At-least-once means the handler **will** run twice one day. That the caller's
handler must be idempotent is their problem; that the retry, the age and the
dead-letter are *visible* is yours — a silently dropped event is the worst bug
this project can ship, because nothing anywhere reports it.

Scaffold state: the queue and the record are modelled; enqueue, the worker loop,
the retry schedule and the dead-letter path raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from .config import Settings
from .models import FunctionConfig, RequestId

__all__ = ["AsyncInvocationQueue", "DeadLetter", "QueuedEvent"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class QueuedEvent:
    """One accepted-but-not-yet-executed invocation.

    `request_id` is stable across every attempt on purpose — the SPEC grades on it.
    A retry that changes the id is indistinguishable from a new event, which makes
    the whole thing untraceable in exactly the situation you most need to trace it.
    """

    function: FunctionConfig
    payload: bytes
    request_id: RequestId
    attempt: int = 1
    # Wall clock, not monotonic: event age is compared against a policy expressed
    # in hours, and it must survive a process restart to mean anything.
    enqueued_at: float = field(default_factory=time.time)
    # When this attempt becomes eligible to run; the backoff sets it.
    visible_at: float = field(default_factory=time.time)
    last_error: str | None = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.enqueued_at


@dataclass(slots=True)
class DeadLetter:
    """An event that ran out of attempts or out of time.

    Carries the original payload, the error and the attempt count — the SPEC
    requires all three, because a DLQ entry you cannot replay or explain is just a
    more expensive way to drop the event.
    """

    event: QueuedEvent
    reason: str
    attempts: int
    dead_lettered_at: float = field(default_factory=time.time)


class AsyncInvocationQueue:
    """The internal queue behind `X-Amz-Invocation-Type: Event`.

    Bounded, on purpose and by the SPEC: a producer faster than the consumer must
    be told so, not buffered until the node dies. That is the difference between
    backpressure and a memory leak.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dead_letters: list[DeadLetter] = []
        # TODO(V5): the queue itself, plus the worker task handles.
        #
        # A plain `asyncio.Queue(maxsize=settings.async_queue_size)` gets you the
        # bound, but note what it does NOT give you: delayed visibility. A retry
        # must not be re-executed until its backoff has elapsed, and re-queuing it
        # at the tail is not the same thing (it runs as soon as it is reached,
        # which under low load is immediately). A heap ordered by `visible_at` is
        # the usual answer — you built one in project 04.

    async def enqueue(self, function: FunctionConfig, payload: bytes) -> RequestId:
        """Accept an event and return immediately. The 202 path.

        Must return BEFORE the handler runs — the SPEC has a test for the ordering,
        because "async" that blocks on the first execution is just a slow sync call.
        """
        # TODO(V5): validate the payload against `max_async_payload_bytes`, mint a
        # request id, enqueue. When the queue is FULL, refuse explicitly (a
        # throttle the caller can see) rather than awaiting a free slot — an
        # `await queue.put()` here silently turns your fast async path into a
        # blocking one under load, which is the exact failure the bound exists to
        # prevent.
        raise NotImplementedError("V5: validate, mint a request id, and enqueue the event")

    async def run_worker(self) -> None:
        """The consumer loop. Started by the lifespan, cancelled on shutdown.

        One of these per worker; how many you run is a concurrency decision that
        must respect V4's governor rather than routing around it.
        """
        # TODO(V5): pull the next VISIBLE event, take a concurrency lease, invoke,
        # and dispatch on the outcome:
        #
        #   success            -> done
        #   throttled          -> requeue WITHOUT consuming an attempt; a throttle
        #                         is capacity, not failure, and burning a retry on
        #                         it is how a busy minute turns into a DLQ full of
        #                         events that never actually ran
        #   function error     -> retry with backoff, or dead-letter if this was
        #                         the last attempt
        #   too old            -> dead-letter without running it
        #
        # Make this cancellation-safe: a SIGTERM mid-invocation must not lose an
        # acknowledged event (the horizontal checklist grades this).
        raise NotImplementedError("V5: consume visible events, invoke, retry or dead-letter")

    def backoff_seconds(self, attempt: int) -> float:
        """How long before `attempt` may run. Exponential, and jittered."""
        # TODO(V5): exponential from `async_retry_base_seconds`. Add JITTER — a
        # burst of events that all fail together will otherwise all retry together,
        # in a synchronised wave, forever. You built this argument in project 01's
        # boss fight; it is the same one.
        raise NotImplementedError("V5: exponential backoff with jitter for this attempt")

    async def dead_letter(self, event: QueuedEvent, reason: str) -> None:
        """Send an exhausted event to its failure destination."""
        # TODO(V5): record it. In-memory is an acceptable start; the horizontal
        # checklist eventually wants it to survive a restart, since a DLQ that is
        # lost on deploy is not a DLQ. Emit a metric and a log line either way —
        # this is the single most important thing in this module to make visible.
        raise NotImplementedError("V5: deliver the event to the dead-letter destination")

    def depth(self) -> int:
        """Queued events — the queue-depth metric, and the backpressure signal."""
        raise NotImplementedError("V5: current queue depth")

    def oldest_age_seconds(self) -> float:
        """Age of the oldest queued event — the SPEC's event-age observable."""
        raise NotImplementedError("V5: age of the oldest queued event")
