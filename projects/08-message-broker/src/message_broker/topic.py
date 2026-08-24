"""V3 — Topics & partitioning: trade global ordering for parallelism.

A `Topic` is **N independent partition logs**. The single interesting decision
lives in `partition_for`: which partition does a record go to?

  * **Keyed** records hash their key to a partition, so the *same key always
    lands on the same partition*. That is what preserves per-key order across a
    producer's entire lifetime, and it is the only ordering guarantee a broker
    at this shape can honestly make.
  * **Keyless** records spread (round-robin) so no partition runs hot.

The guarantee this buys, and its price, are the `Done when` criteria: order is
total *within* a partition and *undefined across* partitions; offsets are
per-partition. Partition count is fixed at create time — changing it remaps every
key, so it is a migration, not a setting.

**The Python trap, and it is a nasty one.** The obvious `hash(key) % n` is
*wrong here*, and wrong in a way that passes every test you write in one process.
CPython salts the hash of `str` and `bytes` with a per-process random seed
(PEP 456) — so `hash(b"user-42")` differs between runs unless `PYTHONHASHSEED` is
pinned. Ship that and a broker restart silently remaps every key to a different
partition: per-key ordering breaks across the restart boundary, and nothing
raises. `hash()` is randomised precisely so it cannot be relied on this way.

What you want is a *stable* hash — one whose output is a function of the bytes
and nothing else. `zlib.crc32(key)` (fast, stdlib, already imported for the
framing) and `hashlib.blake2b(key, digest_size=8)` (slower, far better
distribution) are both fine; the choice, and the reason, go in
`docs/08-design.md`. Note that Kafka itself uses murmur2 for exactly this
property.

The keyless case needs no atomics: a bare `int` counter incremented on the event
loop is safe here, because `partition_for` never awaits and so can never be
interleaved. `itertools.count()` is the idiomatic monotonic source if you prefer
it. Contrast the Rust scaffold's `AtomicU64` — the atomicity was doing real work
there and is redundant here, which is worth understanding rather than copying.
"""

from __future__ import annotations

from pathlib import Path

from .errors import InvalidRequest, TopicAlreadyExists, UnknownPartition
from .log import LogConfig
from .partition import Partition
from .record import Record

__all__ = ["Topic"]


class Topic:
    """A topic: a name and its fixed set of partition logs."""

    def __init__(self, name: str, partitions: list[Partition]) -> None:
        self._name = name
        self._partitions = partitions
        # TODO(V3): the cursor for round-robin placement of keyless records.
        # A plain int is enough (see the module docstring on why no atomic is
        # needed); `itertools.count()` if you want the iterator form.

    @classmethod
    def create(cls, root: Path, name: str, partition_count: int, config: LogConfig) -> Topic:
        """Create a new topic with `partition_count` partitions under
        `root/<name>/<p>/`. Errors if the topic directory already exists."""
        if partition_count < 1:
            raise InvalidRequest("partition count must be >= 1")
        directory = root / name
        if directory.exists():
            raise TopicAlreadyExists()
        return cls(name, _open_partitions(directory, partition_count, config))

    @classmethod
    def open(cls, root: Path, name: str, config: LogConfig) -> Topic:
        """Reopen an existing topic on startup by counting its partition
        directories.

        The directory listing *is* the partition count — there is no metadata
        file to drift out of sync with what is actually on disk.
        """
        directory = root / name
        count = sum(1 for child in directory.iterdir() if child.is_dir())
        return cls(name, _open_partitions(directory, max(count, 1), config))

    @property
    def name(self) -> str:
        return self._name

    @property
    def partition_count(self) -> int:
        return len(self._partitions)

    @property
    def partitions(self) -> list[Partition]:
        return self._partitions

    def partition(self, partition_id: int) -> Partition:
        """Look up a partition by index (used by the fetch route)."""
        if not 0 <= partition_id < len(self._partitions):
            raise UnknownPartition()
        return self._partitions[partition_id]

    def partition_for(self, key: bytes | None) -> int:
        """Choose the partition for a record. **The** V3 decision.

        TODO(V3):
          * `key is not None` -> map it to a stable partition,
            `stable_hash(key) % self.partition_count`. Stable means "same answer
            next process" — read the module docstring before you reach for
            `hash()`, which is not.
          * `key is None` -> take the next round-robin slot so no partition runs
            hot by default.

        Keep it pure and synchronous: it is called once per record on the produce
        path, and anything it touches that is not these two inputs is a bug
        waiting for a restart.
        """
        raise NotImplementedError("V3: keyed stable-hash partitioning + keyless round-robin")

    async def produce(self, record: Record) -> tuple[int, int]:
        """Produce a record: pick a partition, append, return `(partition,
        offset)`.

        Wiring on top of the V3 partitioner and the V1 append — the two things
        this method composes are the two things you have to build.
        """
        partition_id = self.partition_for(record.key)
        offset = await self.partition(partition_id).append(record)
        return partition_id, offset

    async def flush(self) -> None:
        """Durably flush every partition (graceful shutdown)."""
        for partition in self._partitions:
            await partition.flush()


def _open_partitions(directory: Path, count: int, config: LogConfig) -> list[Partition]:
    """Open partitions `0..count`, each in its own subdirectory."""
    return [Partition.open(directory / str(i), i, config) for i in range(count)]
