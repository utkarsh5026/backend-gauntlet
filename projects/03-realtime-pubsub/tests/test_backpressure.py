"""V2 proofs — the slow-consumer problem.

Each test here maps to a "Done when ALL true" criterion in SPEC.md V2: the
mailbox is bounded, the policy switch is honored, a stalled reader costs bounded
memory and does not touch anyone else, and every shed message is counted.
"""

from __future__ import annotations

import asyncio
import time

from conftest import broadcast, drain, mailbox, payload_of, payloads
from prometheus_client import REGISTRY

from realtime_pubsub.backpressure import DeliverOutcome, OverflowPolicy


def dropped_total(policy: OverflowPolicy) -> float:
    """Current value of the drop counter for one policy.

    `get_sample_value` is the registry's public read path — reaching into a
    metric's internals works until prometheus_client changes them.
    """
    value = REGISTRY.get_sample_value(
        "realtime_pubsub_messages_dropped_total", {"policy": policy.value}
    )
    return value or 0.0


def test_drop_newest_sheds_once_full_and_buffer_stays_bounded() -> None:
    box = mailbox(2, OverflowPolicy.DROP_NEWEST)

    assert box.deliver(broadcast("room", "a")) is DeliverOutcome.DELIVERED
    assert box.deliver(broadcast("room", "b")) is DeliverOutcome.DELIVERED
    # Full at capacity 2 — the newest message is shed, not buffered.
    assert box.deliver(broadcast("room", "c")) is DeliverOutcome.DROPPED

    assert box.qsize() == 2
    assert payloads(box) == ["a", "b"]


def test_disconnect_policy_signals_disconnect_and_never_evicts() -> None:
    box = mailbox(1, OverflowPolicy.DISCONNECT)

    assert box.deliver(broadcast("room", "first")) is DeliverOutcome.DELIVERED
    assert box.deliver(broadcast("room", "second")) is DeliverOutcome.DISCONNECT

    # Disconnect never makes room for the new message by evicting the old one.
    assert payloads(box) == ["first"]


def test_drop_oldest_evicts_the_front_so_the_newest_survive() -> None:
    box = mailbox(2, OverflowPolicy.DROP_OLDEST)

    # DROP_OLDEST never reports DROPPED to the publisher — it always makes room
    # by evicting, so every call reports DELIVERED.
    for payload in ("a", "b", "c", "d"):
        assert box.deliver(broadcast("room", payload)) is DeliverOutcome.DELIVERED

    # Only the last `capacity` messages survive; "a" and "b" were evicted from
    # the front to make room, oldest first.
    assert box.qsize() == 2
    assert payloads(box) == ["c", "d"]


def test_fast_drainer_never_sees_a_drop_under_sustained_load() -> None:
    box = mailbox(1, OverflowPolicy.DROP_NEWEST)

    # Capacity 1 would overflow immediately under any backlog — but a drainer
    # that keeps up never lets one form, so every outcome must be DELIVERED.
    for i in range(1_000):
        assert box.deliver(broadcast("room", f"msg-{i}")) is DeliverOutcome.DELIVERED
        message = box.try_recv()
        assert payload_of(message) == f"msg-{i}"


def test_deliver_never_blocks_on_a_stalled_reader_under_any_policy() -> None:
    """The V2 invariant, and a regression guard on the rule in `hub.py`:
    `deliver` is `put_nowait`-based, so it can never wait on a full outbox. If
    it ever became an `await put`, this loop would hang."""
    for policy in OverflowPolicy:
        box = mailbox(1, policy)

        started = time.perf_counter()
        for i in range(10_000):
            # Once DISCONNECT fires the connection is logically gone, but
            # nothing stops the publisher calling deliver again (the hub reaps
            # lazily) — so this must still return promptly under every policy.
            box.deliver(broadcast("room", f"msg-{i}"))
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0, f"deliver under {policy} took {elapsed:.2f}s — looks blocked"


def test_stalled_reader_is_counted_while_a_fast_one_is_unaffected() -> None:
    """The V2 payoff: one connection stalls, another stays caught up, and the
    stalled one's losses never touch the fast one — nor grow its memory."""
    stalled = mailbox(1, OverflowPolicy.DROP_NEWEST)
    fast = mailbox(1_000, OverflowPolicy.DROP_NEWEST)

    before = dropped_total(OverflowPolicy.DROP_NEWEST)

    total = 500
    dropped = 0
    for i in range(total):
        message = broadcast("room", f"msg-{i}")
        if stalled.deliver(message) is DeliverOutcome.DROPPED:
            dropped += 1
        assert fast.deliver(message) is DeliverOutcome.DELIVERED

    # The first delivery fills the stalled mailbox's one slot; every delivery
    # after that is shed — the loss climbs in lockstep with the backlog.
    assert dropped == total - 1
    # Bounded memory: the stalled mailbox never grew past its capacity.
    assert stalled.qsize() == 1
    # And the loss is observable, never silent.
    assert dropped_total(OverflowPolicy.DROP_NEWEST) - before == dropped

    assert len(drain(fast)) == total, "fast subscriber must receive every message"


def test_closed_mailbox_reports_disconnect() -> None:
    box = mailbox(4, OverflowPolicy.DROP_NEWEST)
    box.close()

    assert box.deliver(broadcast("room", "hi")) is DeliverOutcome.DISCONNECT


async def test_recv_drains_buffered_messages_then_reports_closed() -> None:
    """Drain-then-close: a message delivered right before `close()` must still
    be observed by `recv` before it reports the mailbox finished."""
    box = mailbox(2, OverflowPolicy.DROP_OLDEST)
    assert box.deliver(broadcast("room", "last")) is DeliverOutcome.DELIVERED
    box.close()

    message = await box.recv()
    assert payload_of(message) == "last"
    assert await box.recv() is None


async def test_recv_wakes_on_a_later_delivery() -> None:
    """The lost-wakeup guard: `recv` parks on an empty mailbox and must be woken
    by a `deliver` that happens afterwards."""
    box = mailbox(4, OverflowPolicy.DROP_NEWEST)

    waiter = asyncio.create_task(box.recv())
    await asyncio.sleep(0)  # let the task reach the wait
    assert not waiter.done()

    box.deliver(broadcast("room", "late"))
    message = await asyncio.wait_for(waiter, timeout=1.0)
    assert payload_of(message) == "late"


async def test_recv_wakes_on_close() -> None:
    """A parked `recv` must also be woken by teardown, or the writer task leaks
    for the lifetime of the process."""
    box = mailbox(4, OverflowPolicy.DROP_NEWEST)

    waiter = asyncio.create_task(box.recv())
    await asyncio.sleep(0)
    assert not waiter.done()

    box.close()
    assert await asyncio.wait_for(waiter, timeout=1.0) is None
