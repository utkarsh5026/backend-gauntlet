"""V1 — The fan-out hub: the in-process pub/sub core, from scratch.

This is the registry you would normally get from a broadcast channel or an actor
framework. It maps **topic -> subscribers** and, on a publish, hands the message
to every current subscriber's `Mailbox`.

## Where the Rust lesson lands in Python

The Rust version guarded the map with an `RwLock`, and the cardinal rule was
**never hold the lock while you send**: copy out the subscribers, release the
lock, *then* deliver. Hold it across a delivery to a slow client and you have
serialised the entire hub behind that one client.

There is no lock here, and that is not a shortcut — it is the same lesson in
Python's concurrency model. Every method below is a plain `def` with no `await`
in it, so once one starts running, the event loop cannot switch to another task
until it returns. The map is therefore never observed half-mutated, and the
`RwLock` has nothing left to protect.

That guarantee is *bought with a rule*, and the rule is the V1 discipline:

> **Nothing on the publish path may `await`.**

The moment `deliver` becomes a coroutine, or someone slips an `await` into the
loop in `publish`, the loop can suspend mid-fan-out — and now a slow client can
stall every other subscriber, which is precisely the failure the Rust lock
discipline existed to prevent. Same bug, same shape, reached by a different
road. `Mailbox.deliver` uses `put_nowait` for exactly this reason, and
`publish` still snapshots the subscriber list before delivering so the
discipline is visible in the code rather than merely true by accident.

(The other half of the Rust rule — a `Mutex` for cross-*thread* safety — is not
needed because the hub is only ever touched from the event loop thread. If you
ever call it from a `run_in_executor` worker, that changes, and the answer is
`loop.call_soon_threadsafe`, not a lock.)
"""

from __future__ import annotations

from .backpressure import DeliverOutcome, Mailbox
from .protocol import ConnId, ServerMessage, Topic

__all__ = ["Hub"]


class Hub:
    """The in-process subscription registry: topic -> {conn: mailbox}."""

    __slots__ = ("_topics",)

    def __init__(self) -> None:
        self._topics: dict[Topic, dict[ConnId, Mailbox]] = {}

    def subscribe(self, topic: str, conn: ConnId, mailbox: Mailbox) -> None:
        """Add `conn` as a subscriber of `topic`, creating the topic if this is
        its first subscriber.

        Idempotent: re-subscribing the same connection replaces its mailbox
        rather than duplicating the entry.
        """
        self._topics.setdefault(topic, {})[conn] = mailbox

    def unsubscribe(self, topic: str, conn: ConnId) -> None:
        """Remove `conn` from `topic`, pruning the topic once its last
        subscriber leaves.

        Pruning is not cosmetic: topic names are client-controlled, so a hub
        that kept an empty dict per topic ever subscribed to would grow without
        bound under a client that subscribes to a million random names.
        """
        subscribers = self._topics.get(topic)
        if subscribers is None:
            return
        subscribers.pop(conn, None)
        if not subscribers:
            del self._topics[topic]

    def publish(self, topic: str, msg: ServerMessage) -> int:
        """Deliver `msg` to every current subscriber of `topic`.

        Returns how many subscribers it actually reached — which is *not* the
        same as how many are subscribed, because the overflow policy may shed a
        message for a client that is behind. That gap is the number V2 is about.
        """
        subscribers = self._topics.get(topic)
        if not subscribers:
            return 0

        # Snapshot before delivering. Nothing can mutate the map underneath us
        # today (see the module docstring), but taking the copy keeps the
        # never-deliver-while-holding-the-map discipline explicit — and it is
        # what makes the reap below safe, since that *does* mutate.
        targets = list(subscribers.items())

        delivered = 0
        wedged: list[ConnId] = []
        for conn, mailbox in targets:
            match mailbox.deliver(msg):
                case DeliverOutcome.DELIVERED:
                    delivered += 1
                case DeliverOutcome.DISCONNECT:
                    wedged.append(conn)
                case DeliverOutcome.DROPPED:
                    pass

        for conn in wedged:
            # A connection whose mailbox is closed or wedged full is gone as far
            # as this topic is concerned. Reaping here means a dead socket stops
            # costing every future publish a failed delivery, without waiting
            # for the connection loop to notice and call `disconnect`.
            # TODO(observability): bump `metrics.SLOW_CLIENT_DISCONNECTS` here —
            # a client reaped for being too slow is a symptom worth graphing,
            # and it is invisible otherwise.
            self.unsubscribe(topic, conn)

        # TODO(observability): bump `metrics.MESSAGES_DELIVERED` by `delivered`.
        # Published-vs-delivered is the pair that makes fan-out loss legible;
        # `metrics.MESSAGES_DROPPED` (already wired in `backpressure`) is only
        # the other half of it.
        return delivered

    def disconnect(self, conn: ConnId) -> None:
        """Remove `conn` from **every** topic it joined.

        Called on socket teardown: a dropped connection must leave nothing
        behind — no dangling subscriber, no empty topic.
        """
        for topic in list(self._topics):
            subscribers = self._topics[topic]
            subscribers.pop(conn, None)
            if not subscribers:
                del self._topics[topic]

    # --- introspection (metrics + tests) ---------------------------------------

    def subscriber_count(self, topic: str) -> int:
        return len(self._topics.get(topic, ()))

    def topic_count(self) -> int:
        return len(self._topics)

    def subscription_count(self) -> int:
        """Total subscriptions across every topic (one connection in three
        topics counts three times) — the `SUBSCRIPTIONS` gauge."""
        return sum(len(subscribers) for subscribers in self._topics.values())
