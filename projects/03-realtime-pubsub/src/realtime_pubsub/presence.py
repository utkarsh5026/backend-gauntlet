"""V3 — Presence: who is currently in each topic.

Presence is **soft state** — only ever an approximation of reality that must
converge as connections come and go. `PresenceRegistry` tracks, per topic, which
connections are present and the display identity each one claimed.

The easy half is the clean lifecycle: `join` on subscribe, `leave` on
unsubscribe. The hard half is *absence*: a client whose laptop lid closes never
sends a leave, and from the server's side "gone" and "quiet" look identical.
`disconnect` covers every teardown path the server actually observes; `touch`
plus `sweep` handle the silent vanish via heartbeat + TTL.

## The clock

`last_seen` is `time.monotonic()`, not `time.time()`. A TTL measured against the
wall clock is a TTL that can be broken by NTP: a clock that steps backwards
makes every member look freshly-seen and the sweep stops evicting; a step
forward evicts a room full of live people at once. Monotonic time only ever
moves forward and is immune to both. The trade-off — the value is meaningless
across processes and unusable as a timestamp — costs nothing here, because
nothing outside this module ever reads it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .protocol import ConnId, Topic

__all__ = ["Member", "PresenceRegistry"]


@dataclass(slots=True)
class Member:
    """One connection's membership in a topic: display name plus liveness stamp."""

    identity: str
    """Display identity claimed for this membership. Client-supplied, so it is
    **never** trusted for anything but display (SPEC security checklist)."""

    last_seen: float = field(default_factory=time.monotonic)
    """Monotonic seconds. Refreshed by `PresenceRegistry.touch`; compared
    against the TTL by `sweep`."""

    def refresh(self) -> None:
        """Mark this member as seen now (heartbeat or any other liveness proof)."""
        self.last_seen = time.monotonic()


class PresenceRegistry:
    """Per-topic presence: who is currently in each room.

    Orthogonal to `Hub`: the hub answers "who receives messages?", this answers
    "who appears in the room list?". Two connections can be in one and not the
    other — a socket that subscribed but whose identity was rejected, or a
    member whose TTL expired while its socket is still technically open.

    Like the hub, every method here is synchronous and `await`-free, so the map
    is never observed half-mutated.
    """

    __slots__ = ("_topic_members",)

    def __init__(self) -> None:
        self._topic_members: dict[Topic, dict[ConnId, Member]] = {}

    def join(self, topic: str, conn: ConnId, identity: str) -> None:
        """Record `conn` as present in `topic` under `identity`.

        Re-joining replaces the previous member (new identity, fresh stamp).
        """
        self._topic_members.setdefault(topic, {})[conn] = Member(identity)

    def leave(self, topic: str, conn: ConnId) -> None:
        """Remove `conn` from `topic`, pruning the topic when it empties.

        Idempotent — an unknown topic or connection is a no-op.
        """
        members = self._topic_members.get(topic)
        if members is None:
            return
        members.pop(conn, None)
        if not members:
            del self._topic_members[topic]

    def members(self, topic: str) -> list[Member]:
        """Snapshot the members currently present in `topic`.

        Empty for an unknown or pruned topic — both mean "nobody here" to
        soft-state presence, and collapsing them keeps callers from having to
        distinguish a room that never existed from one everybody left.
        """
        return list(self._topic_members.get(topic, {}).values())

    def identities(self, topic: str) -> list[str]:
        """Just the display names — what a `presence` frame carries."""
        return [member.identity for member in self._topic_members.get(topic, {}).values()]

    def disconnect(self, conn: ConnId) -> None:
        """Remove `conn` from every topic it appears in, pruning empty topics.

        The anti-ghost catch-all: call on every WebSocket teardown path (clean
        close, protocol error, abrupt drop, server shutdown). Idempotent if
        `conn` was never present.
        """
        for topic in list(self._topic_members):
            members = self._topic_members[topic]
            members.pop(conn, None)
            if not members:
                del self._topic_members[topic]

    def touch(self, conn: ConnId) -> None:
        """Refresh `last_seen` for `conn` in every topic it belongs to.

        No-op if `conn` is not present anywhere. Wired from the client's
        `heartbeat` frame — and worth wiring from *any* traffic that connection
        sends, since a client that is publishing is self-evidently alive.
        """
        for members in self._topic_members.values():
            member = members.get(conn)
            if member is not None:
                member.refresh()

    def sweep(self, ttl: float) -> list[tuple[Topic, list[Member]]]:
        """Evict members whose `last_seen` is at least `ttl` seconds old.

        Returns only the topics that actually lost someone, each paired with the
        **remaining** members (empty if the room cleared out). Unchanged topics
        are omitted so the background task can broadcast a `presence` frame only
        where it would say something new — a sweep that re-broadcast every room
        on every tick would be its own thundering herd.
        """
        cutoff = time.monotonic() - ttl
        changed: list[tuple[Topic, list[Member]]] = []

        for topic in list(self._topic_members):
            members = self._topic_members[topic]
            expired = [conn for conn, member in members.items() if member.last_seen <= cutoff]
            if not expired:
                continue
            for conn in expired:
                del members[conn]
            changed.append((topic, list(members.values())))
            if not members:
                del self._topic_members[topic]

        return changed

    # --- introspection (metrics + tests) ---------------------------------------

    def topics(self) -> list[Topic]:
        """Rooms with at least one member.

        V4 wants this: a node should subscribe to the bus channel for a room
        only while it actually has someone in it.
        """
        return list(self._topic_members)

    def member_count(self) -> int:
        """Total memberships across every room — the `PRESENCE_MEMBERS` gauge."""
        return sum(len(members) for members in self._topic_members.values())
