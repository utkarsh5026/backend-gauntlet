"""One partition = one log behind a lock. Plumbing, not a vertical.

A partition owns a single `Log` (V1/V2) and an `asyncio.Lock`, and that lock is
the broker's concurrency model made explicit: **appends to a partition
serialise** (one writer at a time), while different partitions append
independently. That is the whole reason partition count caps producer
parallelism.

**Why a lock at all, when Python has one event loop?** Because "one thread" only
buys you atomicity *between* awaits. An append is a sequence — read the tail
position, write the frame, update the index, fsync, bump the offset — and the
fsync has to leave the loop (`asyncio.to_thread`) or it stalls every other
connection. The moment it does, another append task runs and interleaves with
the first: two records claiming one offset, or a frame written into the middle of
another. `asyncio.Lock` is what makes the sequence indivisible across those await
points. The GIL does nothing for you here.

The read path deliberately does **not** take that lock — the SPEC grades
"appends serialise while reads stay concurrent". A reader can therefore race the
tail of an in-flight append; the length-and-CRC framing is what makes that safe,
because a half-written frame fails its check and is treated as end-of-log rather
than data. If you decide you want a different model, that is fine — but say
which one and why in `docs/08-design.md`, because "it seemed to work" is not a
concurrency design.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .log import Log, LogConfig
from .record import Record, StoredRecord

__all__ = ["Partition"]


class Partition:
    """A single partition: an id within its topic plus the log holding its
    records."""

    def __init__(self, partition_id: int, log: Log) -> None:
        self._id = partition_id
        self._log = log
        self._write_lock = asyncio.Lock()

    @classmethod
    def open(cls, directory: Path, partition_id: int, config: LogConfig) -> Partition:
        """Open (creating if needed) this partition's log under `directory`."""
        return cls(partition_id, Log.open(directory, config))

    @property
    def id(self) -> int:
        """This partition's index within its topic."""
        return self._id

    @property
    def log(self) -> Log:
        """The underlying log. Exposed for tests and for the retention worker."""
        return self._log

    async def append(self, record: Record) -> int:
        """Append a record, returning the offset it was assigned (V1).

        The single-writer guarantee lives on this line, not inside `Log`.
        """
        async with self._write_lock:
            return await self._log.append(record)

    async def read_from(self, offset: int, max_records: int) -> list[StoredRecord]:
        """Read up to `max_records` records starting at `offset` (V1 + V2).

        No lock: concurrent fetches are the point of a broker.
        """
        return await self._log.read_from(offset, max_records)

    @property
    def log_end_offset(self) -> int:
        """The next offset to be assigned — one half of the consumer-lag metric."""
        return self._log.log_end_offset

    async def flush(self) -> None:
        """Durably flush this partition on shutdown. Takes the write lock so it
        cannot land in the middle of an append."""
        async with self._write_lock:
            await self._log.flush()
