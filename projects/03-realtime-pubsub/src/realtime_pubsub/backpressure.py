"""V2 — Per-connection backpressure (the slow-consumer problem).

Every connection gets one **bounded outbound mailbox**. The hub pushes
broadcasts into it; the connection's writer task drains it to the socket as fast
as that client's TCP can take them.

Bounded is the whole point. If a client reads slowly while messages keep
arriving, the mailbox fills — and `Mailbox.deliver` has to make the call the SPEC
is about: block the publisher (head-of-line blocking, usually wrong for fan-out),
or stay lossy and drop / disconnect.

## Why this is one class and not two

The Rust version needed *two* halves (a cloneable `Mailbox` sender and an
`Outbox` receiver) and, underneath them, *two* different storage backends. That
second backend existed for one reason: `tokio::mpsc` only lets the **receiver**
pop from the front, but `drop_oldest` has to evict from the **producer** side —
`deliver` is called by the hub, not by the writer task. So `drop_oldest` needed a
hand-rolled ring buffer with a `Notify`, an atomic sender count, and a `Weak`
pointer to detect a vanished receiver.

None of that survives the trip. `asyncio.Queue` exposes `get_nowait()` to
whoever holds the queue, so the producer can evict the oldest entry itself and
one bounded queue serves all three policies. And there is no ownership split to
model: passing this object to the hub and to the writer task *is* the clone.
What Rust needed ~150 lines and three synchronisation primitives for is the
`deliver` method below.

## The one invariant that does survive

**`deliver` never awaits.** It is called from `Hub.publish` while that function
is walking a topic's subscriber list, and the moment it awaits, a slow client can
suspend the publisher — which is exactly the head-of-line blocking the whole
vertical exists to prevent. `put_nowait` (not `await put`) is what enforces it,
and the `deliver_never_blocks` test is the regression guard.
"""

from __future__ import annotations

import asyncio
from enum import Enum, StrEnum

from .metrics import MESSAGES_DROPPED
from .protocol import ServerMessage

__all__ = ["DeliverOutcome", "Mailbox", "OverflowPolicy"]


class OverflowPolicy(StrEnum):
    """What to do when a connection's outbox is full. Parsed from `OVERFLOW_POLICY`.

    `StrEnum` so the value doubles as the Prometheus label and pydantic parses
    the env var into it directly — no `FromStr` impl to hand-write.
    """

    DROP_NEWEST = "drop_newest"
    """Drop the message being delivered; keep the buffered backlog."""

    DROP_OLDEST = "drop_oldest"
    """Evict the oldest buffered message to make room for this one."""

    DISCONNECT = "disconnect"
    """Treat a full outbox as a too-slow client and tear the connection down."""


class DeliverOutcome(Enum):
    """The result of trying to hand a message to one connection."""

    DELIVERED = "delivered"
    """Queued for sending."""

    DROPPED = "dropped"
    """Dropped per the overflow policy (counted — it is the V2 metric)."""

    DISCONNECT = "disconnect"
    """The outbox is full (or gone) and the policy says tear this client down."""


class Mailbox:
    """One connection's bounded outbound queue.

    Held by the hub (once per topic the connection subscribes to) and by that
    connection's writer task — the same object, not two halves.
    """

    __slots__ = ("_arrival", "_capacity", "_closed", "_policy", "_queue")

    def __init__(self, capacity: int, policy: OverflowPolicy) -> None:
        self._capacity = max(1, capacity)
        self._policy = policy
        self._queue: asyncio.Queue[ServerMessage] = asyncio.Queue(maxsize=self._capacity)
        self._closed = False
        # Signals `recv` that something changed: a message arrived, or the
        # mailbox was closed. `asyncio.Queue.get()` alone cannot wake on a
        # close, and a sentinel value cannot be pushed into a queue that is
        # already full — which is precisely the state a closing slow client is in.
        self._arrival = asyncio.Event()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def policy(self) -> OverflowPolicy:
        return self._policy

    @property
    def closed(self) -> bool:
        return self._closed

    def qsize(self) -> int:
        """Messages currently buffered. Never exceeds `capacity` — that bound is
        the V2 invariant, and the memory test asserts on it."""
        return self._queue.qsize()

    def deliver(self, msg: ServerMessage) -> DeliverOutcome:
        """Try to enqueue `msg` **without ever blocking the publisher**.

        Applies `self.policy` when the outbox is full and reports the outcome so
        the hub can count drops and reap wedged connections.
        """
        if self._closed:
            return DeliverOutcome.DISCONNECT

        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            match self._policy:
                case OverflowPolicy.DROP_NEWEST:
                    MESSAGES_DROPPED.labels(policy=self._policy.value).inc()
                    return DeliverOutcome.DROPPED
                case OverflowPolicy.DISCONNECT:
                    return DeliverOutcome.DISCONNECT
                case OverflowPolicy.DROP_OLDEST:
                    # The eviction Rust needed a whole second backend for.
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - drained mid-call
                        pass
                    MESSAGES_DROPPED.labels(policy=self._policy.value).inc()
                    self._queue.put_nowait(msg)

        self._arrival.set()
        return DeliverOutcome.DELIVERED

    def close(self) -> None:
        """Mark this mailbox dead and wake a waiting `recv`.

        This is the explicit stand-in for Rust's "the receiver was dropped".
        Python has no destructor you would want to hang connection teardown on,
        so the connection's `finally` block calls this — which means every exit
        path (clean close, protocol error, abrupt drop, shutdown) has to route
        through that block. `routes.py` is where that is enforced.
        """
        self._closed = True
        self._arrival.set()

    def try_recv(self) -> ServerMessage | None:
        """Pop the next buffered message, or `None` if nothing is queued.

        The non-blocking counterpart to `recv`. Useful to a writer that wants to
        coalesce a burst into one socket write (the "write batching" idea in the
        SPEC's From-the-field backlog), and it is what the tests assert against
        when they need to inspect a mailbox without driving the event loop.
        """
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def recv(self) -> ServerMessage | None:
        """Wait for the next queued message, or `None` once the mailbox is
        closed **and drained**.

        Drain-then-close ordering matters: a message delivered a moment before
        `close()` must still reach the socket, so the buffer is always checked
        before the closed flag.
        """
        while True:
            try:
                return self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

            if self._closed:
                return None

            self._arrival.clear()
            # Re-check between `clear()` and `wait()`. There is no `await`
            # between them, so nothing can interleave here — but a `deliver`
            # that landed *before* the clear would have had its flag wiped, and
            # waiting on it now would hang until the next message. This is the
            # classic lost-wakeup, and the re-check is the fix.
            if not self._queue.empty() or self._closed:
                continue

            await self._arrival.wait()
