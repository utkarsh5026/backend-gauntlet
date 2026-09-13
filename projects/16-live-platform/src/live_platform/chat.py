"""V4 — Chat & presence fan-out at scale. `src/live_platform/chat.py`.

Every live channel has a chat, and a viral stream can put 100k people in one
room. This is project 03's WebSocket fan-out, now multi-tenant and pushed hard,
so the failure modes that matter are about **isolation and backpressure**:

1. **Per-channel fan-out.** A message posted in channel A reaches A's viewers
   only, and a firehose channel must not stall a quiet one.
2. **Slow-consumer handling.** A viewer on hotel wifi cannot be allowed to
   back-pressure the broadcaster: each subscriber has a bounded outbox and an
   explicit overflow policy, never unbounded buffering.
3. **Presence + a cross-pod bus.** Viewer counts per channel, and — because the
   platform runs as many pods — a message published on one pod reaches
   subscribers on the others via Redis pub/sub, each pod dropping its own echo.

## There is no `broadcast` channel in asyncio

The Rust leaned on `tokio::sync::broadcast`: one sender per channel, each
subscriber a receiver over a fixed ring, and `RecvError::Lagged` as a free
signal that a subscriber fell behind. asyncio has nothing shaped like that, and
reaching for a library that pretends to would skip the lesson.

The Python shape is explicit. Each subscriber owns a **bounded `asyncio.Queue`**
— its outbox — and a channel is the set of those. Publishing is a loop over the
set calling `put_nowait`, and `put_nowait` raising `asyncio.QueueFull` *is* your
`Lagged`. What you do in that `except` is the overflow policy.

The one thing a publisher must never do is `await queue.put(...)`. On a full
queue that suspends until the slow viewer drains a slot — so the viewer on hotel
wifi is now pacing delivery to everyone else in the room. It is exactly the
back-pressure the SPEC forbids, and it looks correct in every test that does not
include a slow subscriber.

## Leaving is not automatic here

In Rust a subscriber left when its receiver was dropped; `receiver_count()` fell
on its own. Python has no deterministic destructor to hang that on, so a
subscriber that forgets to leave stays in the set forever: presence over-counts,
and every publish keeps filling an outbox nobody will ever read. `subscribe` is
wired as a context manager for that reason — `with hub.subscribe(key) as sub:`
makes leaving unforgettable, including when the socket dies with an exception.

## Across pods

Redis pub/sub is fire-and-forget: a pod disconnected from Redis for a second
misses that second's messages, with no replay. For chat that is usually the right
trade. Know that you made it.

Scaffold state: the hub is constructed and presence reads; join/leave, publish
and the cross-pod bridge are the V4 worklist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

__all__ = ["BUS_CHANNEL", "BusEnvelope", "ChatHub", "ChatMessage", "Subscription"]

BUS_CHANNEL = "live:chat"
"""The Redis pub/sub channel every pod publishes to and subscribes on."""


class ChatMessage(BaseModel):
    """One chat message fanned out to a channel's subscribers."""

    model_config = ConfigDict(frozen=True)

    stream_key: str
    user: str
    """Display name of the sender, already resolved and authorised upstream."""
    body: str
    sent_at_ms: int


class BusEnvelope(BaseModel):
    """A `ChatMessage` as it crosses the Redis bus, stamped with its origin pod.

    The stamp is what lets a pod recognise its own publish coming back to it and
    drop it — without it, every local subscriber gets every message twice.
    """

    model_config = ConfigDict(frozen=True)

    origin_node: str
    message: ChatMessage


@dataclass(eq=False, slots=True)
class Subscription:
    """One viewer's handle on a channel: its bounded outbox.

    `eq=False` keeps identity equality and hashing, so two viewers of the same
    stream are two distinct members of the channel's set rather than one — which
    field-by-field equality would silently collapse them into.
    """

    stream_key: str
    outbox: asyncio.Queue[ChatMessage] = field(repr=False)


class ChatHub:
    """One channel per live stream, plus the cross-pod Redis bus."""

    def __init__(self, redis: Redis, *, node_id: str, outbox_capacity: int) -> None:
        self._redis = redis
        self.node_id = node_id
        """This pod's id, stamped on outbound bus messages (echo suppression)."""
        self.outbox_capacity = outbox_capacity
        """How far a subscriber may fall behind before the overflow policy applies."""
        self._channels: dict[str, set[Subscription]] = {}

    @property
    def active_channels(self) -> int:
        """Channels with at least one local subscriber (a `/status` gauge)."""
        return len(self._channels)

    def local_presence(self, stream_key: str) -> int:
        """Local subscriber count for a channel — presence *on this pod*.

        The cluster-wide number sums this across pods over the bus; that is V4.
        """
        return len(self._channels.get(stream_key, ()))

    @contextmanager
    def subscribe(self, stream_key: str) -> Generator[Subscription]:
        """Join a channel for the duration of a `with` block, and always leave.

        Wired rather than left as a todo, because it encodes the one Python
        lesson that has no Rust counterpart: see "Leaving is not automatic here".
        """
        subscription = self.join(stream_key)
        try:
            yield subscription
        finally:
            self.leave(subscription)

    # ---- V4 worklist: fan-out, backpressure, presence, cross-pod bus -----------

    def join(self, stream_key: str) -> Subscription:
        """Add a subscriber to a channel, creating the channel on first join.

        TODO(V4): the outbox is bounded by `outbox_capacity`. Prefer `subscribe`
        at call sites — it guarantees the matching `leave`.
        """
        raise NotImplementedError("V4: create the bounded outbox, add it to the channel")

    def leave(self, subscription: Subscription) -> None:
        """Remove a subscriber; drop the channel when its last subscriber goes.

        TODO(V4): must be safe to call twice. An empty channel left in the map is
        a slow leak — one entry per stream that ever had a viewer.
        """
        raise NotImplementedError("V4: remove the subscriber, drop an empty channel")

    async def publish(self, message: ChatMessage) -> None:
        """Deliver to local subscribers and forward onto the bus for other pods.

        TODO(V4): local delivery must never wait on a slow subscriber — see the
        module docstring on `put_nowait` versus `put`. Stamp `node_id` on what
        goes to the bus so the receiving side can drop this pod's own echo.
        """
        raise NotImplementedError("V4: local fan-out + publish a BusEnvelope to Redis")

    async def run_bus(self) -> None:
        """The cross-pod bridge: re-deliver other pods' messages locally.

        TODO(V4): subscribe to `BUS_CHANNEL`, drop envelopes this pod originated,
        and fan the rest out to local subscribers. Runs for the process lifetime
        as a background task started in `main`, and must stop promptly when that
        task is cancelled at shutdown.
        """
        raise NotImplementedError("V4: consume the Redis bus, re-deliver remote messages")
