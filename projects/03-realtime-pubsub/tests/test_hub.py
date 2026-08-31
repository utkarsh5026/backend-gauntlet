"""V1 proofs — the fan-out hub.

Each test maps to a "Done when ALL true" criterion in SPEC.md V1: subscribe /
unsubscribe / publish work and publish reports its reach, disconnect leaves
nothing behind, a stalled subscriber cannot freeze publishes to everyone else,
and concurrent traffic leaves no dangling subscriber.

On the concurrency tests: the Rust versions spawned OS threads, because there
the hub was genuinely shared across a thread pool. Here the hub lives on the
event loop, so the equivalent — and the thing that can actually go wrong — is
many *tasks* interleaving at their await points. The assertions are the same
either way, because they are about the outcome (no leaks, no ghosts) rather
than the mechanism.
"""

from __future__ import annotations

import asyncio

from conftest import broadcast, drain, mailbox, payloads

from realtime_pubsub.backpressure import OverflowPolicy
from realtime_pubsub.hub import Hub
from realtime_pubsub.protocol import next_conn_id


def test_publish_to_unknown_topic_reaches_nobody() -> None:
    hub = Hub()
    assert hub.publish("empty", broadcast("empty", "hi")) == 0


def test_subscribe_and_publish_reaches_all_subscribers() -> None:
    hub = Hub()
    conn_a, conn_b = next_conn_id(), next_conn_id()
    box_a = mailbox(4, OverflowPolicy.DROP_NEWEST)
    box_b = mailbox(4, OverflowPolicy.DROP_NEWEST)

    hub.subscribe("room", conn_a, box_a)
    hub.subscribe("room", conn_b, box_b)

    assert hub.publish("room", broadcast("room", "hello")) == 2
    assert payloads(box_a) == ["hello"]
    assert payloads(box_b) == ["hello"]


def test_subscribing_the_same_conn_twice_is_idempotent() -> None:
    hub = Hub()
    conn = next_conn_id()
    box = mailbox(4, OverflowPolicy.DROP_NEWEST)

    hub.subscribe("room", conn, box)
    hub.subscribe("room", conn, box)

    assert hub.subscriber_count("room") == 1
    assert hub.publish("room", broadcast("room", "once")) == 1
    assert len(drain(box)) == 1


def test_unsubscribe_stops_future_deliveries() -> None:
    hub = Hub()
    conn_a, conn_b = next_conn_id(), next_conn_id()
    box_a = mailbox(4, OverflowPolicy.DROP_NEWEST)
    box_b = mailbox(4, OverflowPolicy.DROP_NEWEST)

    hub.subscribe("room", conn_a, box_a)
    hub.subscribe("room", conn_b, box_b)
    hub.unsubscribe("room", conn_a)

    assert hub.subscriber_count("room") == 1
    assert hub.publish("room", broadcast("room", "after")) == 1
    assert drain(box_a) == []
    assert payloads(box_b) == ["after"]


def test_unsubscribe_prunes_the_empty_topic() -> None:
    """No leaked entries, no empty topics growing forever."""
    hub = Hub()
    conn = next_conn_id()

    hub.subscribe("room", conn, mailbox(4, OverflowPolicy.DROP_NEWEST))
    hub.unsubscribe("room", conn)

    assert hub.subscriber_count("room") == 0
    assert hub.topic_count() == 0
    assert hub.publish("room", broadcast("room", "ghost")) == 0


def test_unsubscribe_from_an_unknown_topic_is_a_noop() -> None:
    hub = Hub()
    hub.unsubscribe("missing", next_conn_id())
    assert hub.topic_count() == 0


def test_disconnect_removes_the_conn_from_every_topic() -> None:
    hub = Hub()
    conn, other = next_conn_id(), next_conn_id()
    box = mailbox(4, OverflowPolicy.DROP_NEWEST)
    other_box = mailbox(4, OverflowPolicy.DROP_NEWEST)

    hub.subscribe("room1", conn, box)
    hub.subscribe("room2", conn, mailbox(4, OverflowPolicy.DROP_NEWEST))
    hub.subscribe("room1", other, other_box)

    hub.disconnect(conn)

    assert hub.subscriber_count("room1") == 1
    assert hub.subscriber_count("room2") == 0
    assert hub.topic_count() == 1
    assert hub.publish("room1", broadcast("room1", "still")) == 1
    assert hub.publish("room2", broadcast("room2", "gone")) == 0
    assert drain(box) == []
    assert payloads(other_box) == ["still"]


def test_publish_does_not_stall_on_a_stalled_subscriber() -> None:
    """The V1 lock-discipline criterion, in Python terms: a subscriber that
    never drains must not slow — or lose a message for — anyone else."""
    hub = Hub()
    stalled, fast = next_conn_id(), next_conn_id()

    # Capacity 1, never drained: every publish after the first finds it full.
    hub.subscribe("room", stalled, mailbox(1, OverflowPolicy.DROP_NEWEST))
    # Generous capacity so the fast subscriber never overflows even though we
    # only drain it after the whole burst.
    fast_box = mailbox(1_000, OverflowPolicy.DROP_NEWEST)
    hub.subscribe("room", fast, fast_box)

    total = 500
    for i in range(total):
        hub.publish("room", broadcast("room", f"msg-{i}"))

    assert len(drain(fast_box)) == total


def test_publish_reaps_a_subscriber_whose_mailbox_was_closed() -> None:
    """Once a subscriber's mailbox is closed (the socket is gone), publish must
    stop delivering to it and prune it — without anyone calling `disconnect`."""
    hub = Hub()
    gone, alive = next_conn_id(), next_conn_id()
    gone_box = mailbox(4, OverflowPolicy.DROP_NEWEST)
    alive_box = mailbox(4, OverflowPolicy.DROP_NEWEST)

    hub.subscribe("room", gone, gone_box)
    hub.subscribe("room", alive, alive_box)

    # Simulate the connection disappearing without a clean teardown.
    gone_box.close()

    assert hub.publish("room", broadcast("room", "hi")) == 1
    assert hub.subscriber_count("room") == 1
    assert payloads(alive_box) == ["hi"]

    # A second publish confirms `gone` stays pruned, not just skipped once.
    assert hub.publish("room", broadcast("room", "again")) == 1


async def test_concurrent_subscribe_publish_unsubscribe_leaves_nothing_behind() -> None:
    async def churn() -> None:
        for i in range(50):
            conn = next_conn_id()
            topic = f"room-{i % 4}"
            hub.subscribe(topic, conn, mailbox(4, OverflowPolicy.DROP_NEWEST))
            await asyncio.sleep(0)  # yield: let the other tasks interleave here
            hub.publish(topic, broadcast(topic, "load"))
            await asyncio.sleep(0)
            hub.unsubscribe(topic, conn)

    hub = Hub()
    await asyncio.gather(*(churn() for _ in range(8)))

    assert hub.topic_count() == 0
    assert hub.subscription_count() == 0


async def test_concurrent_subscribe_publish_disconnect_leaves_nothing_behind() -> None:
    """Mirrors the test above but tears connections down via `disconnect`, the
    multi-topic removal path."""

    async def churn() -> None:
        for i in range(50):
            conn = next_conn_id()
            topic = f"room-{i % 4}"
            hub.subscribe(topic, conn, mailbox(4, OverflowPolicy.DROP_NEWEST))
            await asyncio.sleep(0)
            hub.publish(topic, broadcast(topic, "load"))
            await asyncio.sleep(0)
            hub.disconnect(conn)

    hub = Hub()
    await asyncio.gather(*(churn() for _ in range(8)))

    assert hub.topic_count() == 0
    assert hub.subscription_count() == 0


async def test_publish_racing_disconnect_on_the_same_conn_never_leaks() -> None:
    """The sharp edge: one task keeps publishing to a topic while another
    disconnects the *same* connection. Whatever the interleaving, this must
    never raise and must leave no dangling subscriber or non-empty topic."""
    hub = Hub()
    conn = next_conn_id()
    hub.subscribe("room", conn, mailbox(4, OverflowPolicy.DROP_NEWEST))

    async def publisher() -> None:
        for _ in range(2_000):
            hub.publish("room", broadcast("room", "race"))
            await asyncio.sleep(0)

    async def disconnector() -> None:
        await asyncio.sleep(0)
        hub.disconnect(conn)

    await asyncio.gather(publisher(), disconnector())

    assert hub.subscriber_count("room") == 0
    assert hub.topic_count() == 0
