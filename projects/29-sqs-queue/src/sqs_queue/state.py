"""The queue store and the objects every handler needs, assembled once.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).

Everything here is **plumbing**, and in-memory on purpose: durability is projects
04 and 08, replication is 07 and 09, and a database here would add a dependency
to every test while buying nothing this SPEC grades. Storing a message is not a
vertical; deciding who may delete it, in what order, and when it comes back are
all six of them.

Two pieces are worth reading before you start.

`Queue.counts` is maintained **incrementally**, never by counting. That is not
premature optimization — it is the reason `ApproximateNumberOfMessages` is called
*approximate* in the real service. A number you compute by walking a million
messages is a number you cannot afford to publish every fifteen seconds, so
everyone who has built this has ended up maintaining counters that are cheap and
occasionally slightly wrong. Knowing *why* the AWS console lies to you a little
is worth more than the counters themselves.

`QueueStore.deleted_at` exists so a deleted queue's name stays unusable for a
minute. It looks like bureaucracy and it is a correctness property: an in-flight
request holding the old queue's URL must not land in a *new* queue that happens
to share its name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from .config import Settings
from .control import ControlPlane
from .dedup import DedupWindow
from .errors import OverLimit, QueueDeletedRecently, QueueDoesNotExist
from .fifo import GroupIndex, is_fifo_name
from .inflight import InflightTable, ReceiptHandleCodec
from .models import Message, MessageState, QueueAttributes, QueueKind
from .polling import WaitSet
from .timers import DeadlineEngine

__all__ = ["AppState", "MessageCounts", "Queue", "QueueStore"]

log = structlog.get_logger(__name__)

# How long a deleted queue's name stays reserved. Real SQS says 60 seconds.
QUEUE_NAME_COOLDOWN_SECONDS = 60.0


@dataclass(slots=True)
class MessageCounts:
    """The three approximate gauges, maintained in `O(1)`.

    Every state transition adjusts these; nothing ever counts. If you find
    yourself writing `sum(1 for m in messages if ...)` to produce one of these
    numbers, you have just made the metrics endpoint the most expensive route in
    the service.
    """

    available: int = 0
    inflight: int = 0
    delayed: int = 0

    def apply(self, old: MessageState | None, new: MessageState | None) -> None:
        """Move one message between states. The only way these numbers change."""
        for state, delta in ((old, -1), (new, 1)):
            if state is MessageState.AVAILABLE:
                self.available += delta
            elif state is MessageState.INFLIGHT:
                self.inflight += delta
            elif state is MessageState.DELAYED:
                self.delayed += delta


@dataclass(slots=True)
class Queue:
    """One queue: its identity, its configuration, and its messages."""

    name: str
    kind: QueueKind
    attributes: QueueAttributes
    created_at: float
    url: str
    arn: str
    messages: dict[str, Message] = field(default_factory=dict[str, Message])
    counts: MessageCounts = field(default_factory=MessageCounts)

    @property
    def is_fifo(self) -> bool:
        return self.kind is QueueKind.FIFO

    def oldest_sent_at(self) -> float | None:
        """The send time of the oldest message still here.

        Backs `ApproximateAgeOfOldestMessage` — **the** lag signal for this
        service. Implemented as a scan because the scaffold has no index for it;
        if you put this on a dashboard scraped every 15 seconds, that scan is the
        first thing the boss fight will punish, and maintaining it incrementally
        alongside `counts` is the fix.
        """
        live = [m.sent_at for m in self.messages.values() if m.state is not MessageState.DELETED]
        return min(live) if live else None


class QueueStore:
    """Every queue on this node.

    Fully implemented: creation, lookup, deletion and the name cooldown are
    plumbing. What is *not* here is any decision — whether a re-create conflicts,
    whether an attribute is valid, whether a message may be delivered. Those are
    V6's, V6's and V1/V4's respectively, and keeping them out of the store is
    what stops this class from quietly becoming the whole project.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.queues: dict[str, Queue] = {}
        # name -> when it was deleted, for the 60-second cooldown.
        self.deleted_at: dict[str, float] = {}

    def create(self, name: str, attributes: QueueAttributes, *, now: float | None = None) -> Queue:
        """Create a queue. Raw creation only — idempotency is V6's rule.

        Raises if the name is in its post-deletion cooldown, or if the node is at
        its queue limit. Both are quota checks rather than decisions, which is
        why they live here.
        """
        moment = time.time() if now is None else now
        deleted = self.deleted_at.get(name)
        if deleted is not None and moment - deleted < QUEUE_NAME_COOLDOWN_SECONDS:
            raise QueueDeletedRecently()
        if len(self.queues) >= self._settings.max_queues:
            raise OverLimit(f"this node is limited to {self._settings.max_queues} queues")

        queue = Queue(
            name=name,
            kind=QueueKind.FIFO if is_fifo_name(name) else QueueKind.STANDARD,
            attributes=attributes,
            created_at=moment,
            url=self._settings.queue_url(name),
            arn=self._settings.queue_arn(name),
        )
        self.queues[name] = queue
        self.deleted_at.pop(name, None)
        return queue

    def get(self, name: str) -> Queue:
        try:
            return self.queues[name]
        except KeyError:
            raise QueueDoesNotExist(f"queue {name!r} does not exist") from None

    def get_by_url(self, url: str) -> Queue:
        """Resolve a queue URL.

        Deliberately strict: the name is taken from the URL's last segment and
        looked up, so a URL for a deleted queue fails rather than resolving to
        whatever now holds that name.
        """
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise QueueDoesNotExist("malformed queue url")
        return self.get(name)

    def list_names(self, prefix: str | None = None) -> list[str]:
        names = sorted(self.queues)
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        return names

    def delete(self, name: str, *, now: float | None = None) -> None:
        """Delete a queue and start its name's cooldown."""
        if name not in self.queues:
            raise QueueDoesNotExist(f"queue {name!r} does not exist")
        del self.queues[name]
        self.deleted_at[name] = time.time() if now is None else now

    def total_messages(self) -> int:
        """Every live message on the node. For the process-wide gauge."""
        return sum(len(q.messages) for q in self.queues.values())


@dataclass(slots=True)
class AppState:
    """Everything a handler needs, assembled once by `main.build_state`.

    Read the field order as the dependency graph of the SPEC: the codec feeds the
    in-flight table (V1), the deadline engine drives it and the dedup window
    (V2, V5), the wait set sits in front of receives (V3), the group index
    constrains them on FIFO queues (V4), and the control plane configures all of
    it (V6).
    """

    settings: Settings
    store: QueueStore
    codec: ReceiptHandleCodec
    inflight: InflightTable
    deadlines: DeadlineEngine
    waiters: WaitSet
    groups: GroupIndex
    dedup: DedupWindow
    control: ControlPlane
