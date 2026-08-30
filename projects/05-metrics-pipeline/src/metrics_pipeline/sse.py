"""V4 — The SSE live fan-out: push closed windows to many dashboards.

Every time the rollup engine closes a window (V2), that row is broadcast to every
connected `GET /stream` client over **Server-Sent Events**. SSE — not WebSocket —
is the right tool: the traffic is one-directional (server->client), it rides
plain HTTP, and browsers auto-reconnect for free.

The lesson (SPEC V4) is the *inverse* of V3's backpressure. The durable sink must
never drop a rollup. The live view **must be willing to drop**: a slow browser
tab cannot be allowed to back-pressure the pipeline. So each subscriber gets its
own **bounded** queue, and a subscriber whose queue is full is shed — its rows
are dropped and counted — never allowed to stall the producer.

That per-subscriber bounded queue is the shape to internalise, because Python has
no broadcast channel in the stdlib and the naive alternatives are both wrong:

* One shared queue does not fan out — the first consumer to wake takes the row
  and the other dashboards never see it.
* An **unbounded** per-subscriber queue fans out correctly and then OOMs the
  process, because a browser tab that stopped reading is indistinguishable from
  one that is merely slow, and the pipeline keeps producing either way.

Scaffold state: registration is wired (`subscribe`/`unsubscribe` work and
`GET /metrics` can already count subscribers); `publish` and `stream` raise.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import Response

from .model import RollupRow

__all__ = ["LiveFeed", "Subscriber"]


@dataclass(slots=True, eq=False)
class Subscriber:
    """One connected dashboard: a bounded mailbox plus its shed counter.

    `eq=False` keeps the dataclass on identity hashing, which is what a set of
    live connections wants: two subscribers with the same counters are still two
    different browser tabs. (A value-equality dataclass is unhashable, so the
    set below would refuse it outright.)
    """

    queue: asyncio.Queue[RollupRow]
    dropped: int = 0
    """Rows shed because this client fell behind. Export it — "clients dropped
    for lag" is a graded metric, and a subscriber that only ever *silently*
    drops is indistinguishable from a healthy one."""
    last_event_id: int = field(default=0)
    """Monotonic id of the last event handed to this client, for the `id:` field
    the browser echoes back as `Last-Event-ID` when it reconnects."""


class LiveFeed:
    """The fan-out hub: one producer, many SSE subscribers.

    `publish` is called by the pipeline as windows close (the V2 -> V4 hand-off);
    `subscribe` hands each connected client its own mailbox. The mailboxes are
    **bounded** on purpose — that bound is the load-shedding policy.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("sse capacity must be > 0")
        self._capacity = capacity
        """How many rollups a subscriber may fall behind before it starts
        dropping. Tune it against your window-close rate: one window's worth of
        series is the natural unit."""
        self._subscribers: set[Subscriber] = set()

    def subscribe(self) -> Subscriber:
        """Register a new client and hand back its mailbox."""
        sub = Subscriber(queue=asyncio.Queue(maxsize=self._capacity))
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        """Drop a client. Must run even when the connection died mid-write —
        wrap the streaming loop in try/finally or the set leaks a mailbox per
        disconnected browser tab, which is the same OOM by a slower road.
        """
        self._subscribers.discard(sub)

    def publish(self, row: RollupRow) -> None:
        """Broadcast a closed window to every subscriber.

        Non-blocking and infallible by contract: this is called from the pipeline
        loop, and the pipeline's liveness must not depend on the slowest
        dashboard. An idle feed with no subscribers is not an error either.
        """
        # TODO(V4): fan `row` out to `self._subscribers`.
        #   - `queue.put_nowait(row)` per subscriber, catching `asyncio.QueueFull`
        #     — on full, SHED: bump `sub.dropped` and move on. Never `await
        #     queue.put(...)` here; that is precisely the backpressure you are
        #     forbidden from applying.
        #   - decide a shedding policy and write it down in the design doc:
        #     drop the newest row, drop the oldest (`get_nowait()` then put, so
        #     the dashboard sees fresh data rather than stale), or conflate to
        #     keep-latest-per-series. They give visibly different graphs under
        #     load and the choice is yours to defend.
        #   - a client that has been shedding for a while is arguably dead:
        #     consider disconnecting it rather than dropping forever.
        raise NotImplementedError("V4: fan a closed window out to subscribers, shedding slow ones")

    @property
    def subscribers(self) -> int:
        """Current subscriber count — export as the connected-clients gauge."""
        return len(self._subscribers)


async def stream(feed: LiveFeed, last_event_id: str | None) -> Response:
    """Build the SSE response for one `GET /stream` connection (V4).

    `last_event_id` is the browser's `Last-Event-ID` header on reconnect — use it
    to resume without gaps where you can.
    """
    # TODO(V4): return a `StreamingResponse` with media type `text/event-stream`.
    #   - subscribe, then define an async generator that loops on
    #     `await sub.queue.get()` and yields one SSE frame per row. The framing
    #     is text you write yourself — there is no encoder to hide behind:
    #         id: <n>\n
    #         event: rollup\n
    #         data: <one-line JSON>\n
    #         \n
    #     The blank line terminates the event, and `data:` must not contain a
    #     raw newline — `row.model_dump_json()` gives you compact one-line JSON.
    #   - emit `retry: <ms>` once at the top so a dropped browser reconnects on
    #     your schedule rather than the default.
    #   - KEEP-ALIVE: yield a comment line (`: ping\n\n`) on a timeout, so idle
    #     connections survive proxies that reap silent sockets. Wrap the queue
    #     get in `asyncio.wait_for(...)` for that.
    #   - ALWAYS `feed.unsubscribe(sub)` in a `finally`. The generator is closed
    #     (GeneratorExit) when the client disconnects, which is the only signal
    #     you get that the tab was closed.
    #   - set `Cache-Control: no-cache` and `X-Accel-Buffering: no` on the
    #     response — a buffering proxy in front of you will otherwise hold
    #     events until its buffer fills and make the "live" feed arrive in
    #     bursts. This is the classic reason SSE "doesn't work" in production.
    #   - honour `last_event_id` for resume where feasible.
    raise NotImplementedError("V4: stream closed rollup windows to one client over SSE")
