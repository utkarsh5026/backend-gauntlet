"""V4 — Multi-node fan-out: one logical topic across many processes.

A `Hub` only knows about *this* node's sockets. With two nodes behind a load
balancer, a publish on node A never reaches a subscriber whose socket lives on
node B. The bridge closes that gap by carrying messages over a **cross-node bus**
(Redis pub/sub)::

    client -> A  --publish-->  A.hub (local sockets)
                               '--> Redis channel --> B.run() --> B.hub (local sockets)

Two rules make or break it:

1. The receive side injects into the **local hub only** — it must NOT re-publish
   to Redis, or every message loops forever.
2. Each message is stamped with this node's id, so a node recognises and drops
   its own messages coming back around.

Scaffold state: `BusEnvelope` and the client wiring are given; `publish` and
`run` are yours. Turn the bridge on with `CLUSTER=true` and both raise.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
import structlog
from pydantic import BaseModel

from .hub import Hub

__all__ = ["BusEnvelope", "ClusterBridge", "channel_for"]

log = structlog.get_logger(__name__)

CHANNEL_PREFIX = "pubsub:"


def channel_for(topic: str) -> str:
    """Redis channel carrying `topic`. One channel per topic, so a node can
    subscribe to only the rooms it actually has members in (criterion 4)."""
    return f"{CHANNEL_PREFIX}{topic}"


class BusEnvelope(BaseModel):
    """What travels over the bus: the broadcast plus the id of the node that
    originated it (for loop-breaking / de-dup on the receive side)."""

    origin: str
    """`NODE_ID` of the node that first published this. Receivers drop their own."""
    topic: str
    payload: Any


class ClusterBridge:
    """Bridges the local `Hub` to Redis pub/sub.

    Held in `AppState` as `ClusterBridge | None` — `None` is single-node mode
    (V1-V3), where the bus is never touched.
    """

    def __init__(self, redis_url: str, node_id: str, hub: Hub) -> None:
        self.node_id = node_id
        """This node's stable id (`NODE_ID`). Stamped onto every outgoing envelope."""
        self.hub = hub
        """The local hub the receive loop injects bus messages into."""
        # `from_url` builds a client over a connection *pool* and opens nothing
        # yet — the first command connects. Keep this one client for the process
        # rather than making one per publish: a fresh connection on the hot path
        # would put a TCP handshake (and Redis's own accept) inside every
        # broadcast.
        self.client: aioredis.Redis = aioredis.from_url(  # pyright: ignore[reportUnknownMemberType]
            redis_url, decode_responses=True
        )

    async def publish(self, topic: str, payload: Any) -> None:
        """Put a broadcast onto the bus so other nodes can deliver it locally."""
        # TODO(V4): publish a `BusEnvelope(origin=self.node_id, topic=..., payload=...)`
        # to `channel_for(topic)`.
        #   - `await self.client.publish(channel, envelope.model_dump_json())`.
        #     `model_dump_json()` gives compact one-line JSON; the receive side
        #     parses it back with `BusEnvelope.model_validate_json`.
        #   - This is fire-and-forget-ish: catch `redis.RedisError`, log it, and
        #     return. A Redis hiccup should degrade the app to single-node
        #     delivery, NOT fail the publishing client's frame — local
        #     subscribers already got the message before this was called.
        #   - Do not add a retry loop here. This is on the publish path, and an
        #     `await` that waits on a sick Redis is an `await` that stalls the
        #     connection handling the frame.
        raise NotImplementedError("V4: publish a broadcast onto the Redis bus")

    async def run(self) -> None:
        """Background task: subscribe to the bus and inject arriving messages
        into the local hub. Spawned once at startup; runs for the process
        lifetime."""
        # TODO(V4): the receive side of the bridge.
        #   1. `pubsub = self.client.pubsub()`, then
        #      `await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")` — the simple
        #      start. The scalable version subscribes lazily per active topic
        #      (`await pubsub.subscribe(channel_for(t))` when a topic gains its
        #      first local subscriber, `unsubscribe` when it loses its last), so
        #      this node is not firehosed with traffic for rooms nobody here is
        #      in. That needs a hook from `Hub.subscribe`/`unsubscribe` — an
        #      `asyncio.Queue` of subscription changes is the usual shape,
        #      because you cannot await a Redis call from the sync hub methods.
        #   2. Loop with `async for raw in pubsub.listen():` and skip anything
        #      whose `raw["type"]` is not `"message"` / `"pmessage"` — the
        #      subscribe confirmations arrive on the same iterator.
        #   3. Decode with `BusEnvelope.model_validate_json(raw["data"])`.
        #      Anything unparseable is another node speaking a version you do
        #      not know: log it and continue, never crash the loop.
        #   4. LOOP-BREAK: `if envelope.origin == self.node_id: continue`. This
        #      is our own message echoing back and local subscribers already
        #      have it.
        #   5. Inject into the LOCAL hub only:
        #      `self.hub.publish(envelope.topic, BroadcastMessage(topic=..., payload=...))`.
        #      Do NOT call `self.publish(...)` here — that re-emits to the bus
        #      and builds an infinite echo loop.
        #   6. Wrap the whole thing so a dropped Redis connection reconnects with
        #      backoff instead of ending the task. A background task that raises
        #      dies *silently* in asyncio — `main.py` attaches a done-callback to
        #      surface it, but the loop should not need it in the first place.
        raise NotImplementedError("V4: subscribe to the bus and inject into the local hub")

    async def aclose(self) -> None:
        """Release the connection pool on shutdown."""
        await self.client.aclose()
