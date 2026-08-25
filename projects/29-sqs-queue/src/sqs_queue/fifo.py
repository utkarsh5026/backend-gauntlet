"""V4 — FIFO: ordering keys, and the parallelism they buy back.

Start with why strict global FIFO is a trap. If message *n+1* may not be
processed until *n* is done, the queue has exactly one useful consumer. Run
sixteen and fifteen of them wait. You bought ordering by giving up the entire
reason you put a queue there, and no amount of tuning gets it back — the
guarantee and the parallelism are the same resource spent twice.

The universal resolution is to make ordering a property of a **key** rather than
of the queue. Messages sharing a `MessageGroupId` are strictly ordered; messages
in different groups are independent; and the number of distinct groups becomes
your parallelism ceiling. Every system that offers ordering at scale arrived at
this: SQS `MessageGroupId`, Kafka partition keys, Pulsar `Key_Shared`
subscriptions, Pub/Sub ordering keys. Choosing the key well (`order_id` — many,
independent) versus badly (`region` — four groups, so four workers) is the whole
skill, and it is a design decision your *users* make with a field you defined.

The mechanism to work out is what must be true of a group while one of its
messages is in flight. Note the shape of the answer: the constraint is on the
**group**, not on the message, which means a receive is no longer "give me the
oldest available message" — it is "give me the oldest available message from a
group that is not currently blocked". Think about what that costs when there are
a million groups, because the boss fight has a thousand and the real world has
more.

Then face the consequence, which the SPEC asks you to demonstrate rather than
avoid: **head-of-line blocking**. A message at the front of a group that nobody
can process stalls that group until it is deleted or redriven to the DLQ. This is
not a bug — it is the guarantee, seen from the other side. What *would* be a bug
is a blast radius bigger than one group, and that is the criterion: one stuck
group, every other group draining normally.

Two smaller rules complete the contract. Each message gets a **sequence number**
that strictly increases within its group — on the wire it is a decimal string,
because it outgrew 64 bits. And FIFO-ness lives in the queue **name** (`.fifo`),
so the contract is visible at every call site rather than hidden in an attribute
nobody reads.

Scaffold state: the shapes are modelled; group selection, sequencing and the
blocking rule raise.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import Settings
from .models import Message

__all__ = ["FIFO_SUFFIX", "GroupIndex", "GroupState", "is_fifo_name"]

log = structlog.get_logger(__name__)

# The suffix that makes a queue FIFO. Not decoration: a client reading a queue
# URL can tell what it is promised without a `GetQueueAttributes` round trip.
FIFO_SUFFIX = ".fifo"


def is_fifo_name(name: str) -> bool:
    """Whether a queue name declares itself FIFO. Plumbing, and complete."""
    return name.endswith(FIFO_SUFFIX)


@dataclass(slots=True)
class GroupState:
    """One ordering group's bookkeeping.

    `blocked_by` is the head-of-line pointer: while it is set, this group has a
    message in flight and no later message from it may be delivered. When you
    build the stuck-group test, this field is what it asserts on — and the
    *other* groups' copies of it are what proves the blast radius is one.
    """

    group_id: str
    next_sequence: int
    blocked_by: str | None = None
    # How many messages are queued behind the head. The hot-key signal: a group
    # whose depth climbs while others stay flat is a key whose production rate
    # exceeds one consumer, and no amount of extra consumers will fix it.
    depth: int = 0


class GroupIndex:
    """Which groups exist, which are blocked, and what may be delivered next.

    The data structure question is the interesting one. A receive must find
    messages from *unblocked* groups, and the naive implementation walks every
    group looking for one — which is `O(groups)` per receive and turns the
    thousand-group boss scenario into a thousand-element scan on every call.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def next_sequence(self, group_id: str) -> int:
        """The next sequence number for a group. Strictly increasing, never reused.

        "Never reused" is doing work: a group that empties completely and then
        receives a new message must not restart at zero, or a consumer that
        deduplicates on sequence number will silently drop it.
        """
        # TODO(V4): allocate the next sequence number for this group.
        raise NotImplementedError("V4: allocate a per-group sequence number")

    def selectable(self, limit: int) -> list[str]:
        """Groups that currently have a deliverable head, up to `limit`.

        The heart of the vertical. A group qualifies when it has an available
        message and nothing in flight. Getting the answer is easy; getting it
        without walking every group is what the bench measures.
        """
        # TODO(V4): return unblocked groups with available messages, cheaply.
        raise NotImplementedError("V4: select unblocked groups with available messages")

    def block(self, group_id: str, message_id: str) -> None:
        """Mark a group as having a message in flight.

        Called from the receive path, before the message is handed out. Consider
        what happens if a receive fails after this and before the message
        reaches the client — a group blocked by a delivery that never happened
        stays blocked until a lease expires that nobody is holding.
        """
        # TODO(V4): block the group on this message.
        raise NotImplementedError("V4: block a group on an in-flight message")

    def unblock(self, group_id: str, message_id: str) -> None:
        """Release a group when its in-flight message is deleted or expires.

        Takes the message id, not just the group, so a stale unblock — from a
        superseded delivery, exactly the case V1 is about — cannot release a
        group that a *different* delivery is legitimately holding.
        """
        # TODO(V4): release the group only if this message is the one blocking it.
        raise NotImplementedError("V4: unblock a group when its head is released")

    def on_send(self, message: Message) -> None:
        """Record a newly sent message against its group. Called by the send path.

        Also the place to enforce the contract: a FIFO queue **requires** a
        `MessageGroupId` and must refuse a message without one, rather than
        inventing a default group — which would silently serialize every message
        with no group behind a single ordering constraint nobody asked for.
        """
        # TODO(V4): index the message by group and maintain the group's depth.
        raise NotImplementedError("V4: index a sent message by ordering group")
