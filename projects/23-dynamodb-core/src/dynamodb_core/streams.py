"""V5 — Streams.

A store nobody can subscribe to is a dead end. A stream is the table's change log,
and its guarantees are shaped exactly like the table's storage: **strictly ordered
within a partition key**, unordered across them. That is not a weak promise — it is
the strongest one a partitioned store can make without a global sequencer, and it
happens to be precisely what a downstream consumer needs, because anything that
cares about ordering cares about it *per entity*.

This is also the seam the next project plugs into: an event source mapping polls
these shards and batches records into function invocations.

Scaffold state: records and iterators are modelled; append, read and trim raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .item import Item, ItemKey

__all__ = [
    "EventName",
    "ShardIteratorType",
    "Stream",
    "StreamRecord",
    "StreamViewType",
]


class EventName(StrEnum):
    INSERT = "INSERT"
    MODIFY = "MODIFY"
    REMOVE = "REMOVE"


class StreamViewType(StrEnum):
    """How much of the item each record carries.

    The cost dial again: `NEW_AND_OLD_IMAGES` lets a consumer compute a diff
    without reading the table back, at the price of every record carrying two
    copies of the item.
    """

    KEYS_ONLY = "KEYS_ONLY"
    NEW_IMAGE = "NEW_IMAGE"
    OLD_IMAGE = "OLD_IMAGE"
    NEW_AND_OLD_IMAGES = "NEW_AND_OLD_IMAGES"


class ShardIteratorType(StrEnum):
    TRIM_HORIZON = "TRIM_HORIZON"
    LATEST = "LATEST"
    AT_SEQUENCE_NUMBER = "AT_SEQUENCE_NUMBER"
    AFTER_SEQUENCE_NUMBER = "AFTER_SEQUENCE_NUMBER"


@dataclass(slots=True)
class StreamRecord:
    """One change.

    `sequence_number` is monotonic **within a partition key**. Do not promise more
    than that: a global counter would be a single point of contention and would
    imply an ordering across keys that the storage layer never provided.
    """

    sequence_number: str
    event_name: EventName
    key: ItemKey
    created_at: float
    old_image: Item | None = None
    new_image: Item | None = None


class Stream:
    """The change log for one table."""

    def __init__(
        self,
        view_type: StreamViewType,
        retention_seconds: float,
        buffer_size: int,
    ) -> None:
        self._view_type = view_type
        self._retention_seconds = retention_seconds
        self._buffer_size = buffer_size
        # TODO(V5): per-shard record storage, one shard per partition key:
        #
        #   self._shards: dict[bytes, deque[StreamRecord]]
        #
        # A `collections.deque(maxlen=buffer_size)` gives you the bound for free —
        # and note what the bound MEANS: the oldest record is dropped, so a
        # consumer that falls too far behind loses data and must be told (that is
        # the trimmed-iterator error below), not silently served a gap.

    @property
    def view_type(self) -> StreamViewType:
        return self._view_type

    def append(
        self,
        event: EventName,
        key: ItemKey,
        old_image: Item | None,
        new_image: Item | None,
    ) -> StreamRecord:
        """Record one mutation.

        The caller must invoke this atomically with the item write — a committed
        item with no record (or a record with no item) is the bug the V5 crash
        test hunts for.
        """
        # TODO(V5): assign the next sequence number for THIS partition key, cut
        # the images down to what `view_type` promises, and append. A delete of a
        # non-existent item is not a change — append nothing and say so by
        # returning early rather than writing an empty REMOVE.
        raise NotImplementedError("V5: append an ordered record for this partition key")

    def read(
        self,
        iterator: str,
        *,
        limit: int = 100,
    ) -> tuple[list[StreamRecord], str | None]:
        """Read from a shard iterator; returns records and the next iterator.

        A `None` next-iterator means the shard is closed and will never yield more.
        """
        # TODO(V5): decode the iterator (shard + position), return up to `limit`
        # records in order, and encode the next position. If the position points
        # at data that has already been trimmed, raise the distinct
        # "trimmed data" error — silently skipping ahead would hand the consumer
        # a gap it has no way to detect.
        raise NotImplementedError("V5: read a bounded batch from a shard iterator")

    def trim(self, now: float) -> int:
        """Drop records past the retention window; returns how many went."""
        # TODO(V5): retention is time-based, and it is what makes a stream a
        # buffer rather than a log you can replay forever. Called periodically —
        # note that time-based trimming and the deque's size bound are two
        # DIFFERENT limits, and a record can be lost to either.
        raise NotImplementedError("V5: trim records past the retention window")

    def lag(self, iterator: str, now: float) -> float:
        """Seconds between the newest record and where this iterator sits.

        Backs the stream-lag metric and the boss fight's lag target.
        """
        raise NotImplementedError("V5: consumer lag in seconds for this iterator")
