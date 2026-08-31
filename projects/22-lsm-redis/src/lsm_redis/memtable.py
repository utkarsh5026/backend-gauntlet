"""V3 — Memtable: the sorted, in-memory write buffer. `src/lsm_redis/memtable.py`.

Every write lands here, right after the WAL append. This is the "L" of the LSM
tree: a **log-structured** engine turns random writes into sequential ones by
buffering them in memory and only ever writing whole, immutable, sorted files to
disk (SSTables, V4). For that flush to be cheap the buffer has to already be in
key order.

Three semantics beyond "a dict":

1. **Tombstones.** A delete stores `TOMBSTONE`, it does not `del` the key. Older
   values for that key still live in SSTables on disk; the tombstone *shadows*
   them on read until compaction (V6) drops both. Removing the key from the map
   would un-delete it on the next read from disk — the deleted value would come
   back, which is about the worst bug a database can have.
2. **Sequence numbers.** Each entry carries the write's `seq` so that when a key
   exists in the memtable *and* several SSTables, the read path can pick the
   newest. Within one memtable a later write simply overwrites.
3. **Size accounting + rotation.** You track approximate bytes held. Crossing
   `MEMTABLE_MAX_BYTES` **freezes** the buffer (a fresh one takes writes) while
   the frozen one flushes in the background. Freezing instead of blocking is
   what keeps writes flowing during a flush — get that handoff wrong and you get
   the write stall, which is the boss.

*Concept to internalize:* why LSM trades read simplicity for write throughput
(sequential sorted flushes vs a B-tree's in-place random writes), and why a
delete in a log-structured store is an *append*, never an in-place erase.

## Choosing the ordered map, in Python

Rust reached for `BTreeMap` and the choice was made. Python's stdlib has no
sorted mapping, and the three candidates have genuinely different shapes — this
is a real decision, and `docs/22-design.md` should say which you took and why.

* **`dict` + `sorted()` at flush time.** Inserts are O(1) with CPython's fastest
  data structure; the sort is O(n log n) once per flush, in C, over a few
  thousand keys. Since 3.7 a `dict` also preserves insertion order, which is
  *not* key order and will mislead you if you forget. This is very often the
  right answer here, precisely because the memtable is written constantly and
  iterated in order exactly once.
* **`bisect.insort` over a parallel sorted key list.** Ordered at all times, so
  a range scan is free. The insert is O(n) memmove — but it is a C memmove, so
  it beats a Python-level tree until the buffer is large. Measure where.
* **`sortedcontainers.SortedDict`.** The mature third-party answer, and the
  honest comparison for the design doc. It is a dependency, so if you take it,
  take it for a measured reason.

Note what does **not** need solving: `bytes` compares lexicographically in
Python, byte by byte, which is exactly the ordering an SSTable wants. No
comparator, no key function, no encoding decisions.

## What "approximate bytes" means when nothing has a size

Rust could add up the lengths it owned. CPython objects have overhead you do not
control — `sys.getsizeof(b"")` is 33 bytes before a single byte of payload, and
the `dict` entry, the `Entry` object and its slots are all real memory that
`len(key) + len(value)` does not see. Two consequences:

* Pick a formula, write it down, and keep it *consistent* — the number's job is
  to trigger a flush at a predictable point, not to be true.
* Whatever you pick, it will undercount, so RSS will exceed
  `MEMTABLE_MAX_BYTES × (memtables in flight)` by a fixed factor. Measure that
  factor once and put it in `docs/22-benchmarks.md`; it is what turns the boss
  fight's "memory stays bounded" from a hope into a number.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

__all__ = ["TOMBSTONE", "Entry", "Memtable", "Tombstone", "Value"]


class Tombstone:
    """The "deleted here" marker. A single instance, `TOMBSTONE`, is the only
    one you need — it carries no data, and identity comparison (`is`) is both
    the fastest and the clearest test.

    A sentinel object rather than `None`, because `None` is already the answer
    to "this level has no opinion about that key" on the read path. Collapsing
    "not here, keep looking" and "deleted, stop looking" into one value is the
    single most likely way to resurrect a deleted key, so they get different
    types and the type checker enforces the difference.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "TOMBSTONE"


TOMBSTONE: Final = Tombstone()

type Value = bytes | Tombstone
"""What a key maps to: a live value, or the marker that it was deleted."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One key's current state in this buffer, tagged with the write that
    produced it."""

    seq: int
    value: Value

    @property
    def is_tombstone(self) -> bool:
        return self.value is TOMBSTONE


class Memtable:
    """A sorted, in-memory write buffer.

    `__len__`, `approx_bytes` and `is_full` are wired (they feed `/stats` and
    the flush trigger); the mutation, lookup and ordered iteration are V3.
    """

    def __init__(self) -> None:
        self._entries: dict[bytes, Entry] = {}
        self._approx_bytes = 0
        self.frozen = False
        """Set when this buffer has been retired from the write path and is
        waiting to be flushed. A frozen memtable is still *read* — it holds the
        newest value for its keys until its SSTable exists — but takes no
        writes, which is what lets a flush happen without blocking anyone."""

    def __len__(self) -> int:
        """Distinct keys buffered. A tombstone counts: it is an entry."""
        return len(self._entries)

    @property
    def approx_bytes(self) -> int:
        """Approximate heap bytes held — the rotation trigger. See the module
        docstring on why "approximate" is the honest word."""
        return self._approx_bytes

    def is_full(self, max_bytes: int) -> bool:
        return self._approx_bytes >= max_bytes

    def insert(self, key: bytes, value: Value, seq: int) -> None:
        """Apply a write (a value, or `TOMBSTONE` for a delete) at `seq`.

        TODO(V3): upsert the entry and keep `_approx_bytes` in step — add the
        new footprint, subtract the footprint of any entry you replaced. The
        criterion that catches a sloppy version is "overwriting a key does not
        double-count its bytes": write the same key a million times and the
        buffer must stay the size of one key, or a hot-key workload flushes
        constantly for no reason and you have manufactured your own write
        stall.

        A later `seq` for a key supersedes an earlier one. Whether you *enforce*
        that (ignore an out-of-order seq) or merely rely on the engine handing
        them to you in order is a real choice — replay after a crash is where it
        gets tested, so decide deliberately.
        """
        raise NotImplementedError(
            "V3: upsert the entry (TOMBSTONE on delete) and update approx_bytes"
        )

    def get(self, key: bytes) -> Entry | None:
        """This buffer's opinion about `key`, or `None` for "I have none".

        The distinction is the entire read path in one return type: an `Entry`
        holding `TOMBSTONE` is a **positive** answer — "deleted here" — and the
        engine must stop and return a miss rather than falling through to an
        SSTable that still has the old value. `None` means "not here, keep
        looking". Reading this method as "did I find the key" instead of "do I
        have an opinion" is how deleted keys come back from the dead.

        TODO(V3): probe the map. This is one line; the learning is the paragraph
        above it.
        """
        raise NotImplementedError(
            "V3: return this memtable's entry for the key, tombstone included"
        )

    def items_sorted(self) -> Iterator[tuple[bytes, Entry]]:
        """Every entry in ascending key order — exactly the sequence an SSTable
        writer (V4) consumes to produce a sorted file, with tombstones included
        (they travel to disk; only compaction drops them).

        TODO(V3): yield the entries in key order. Which line this is depends on
        the structure you chose in the module docstring — that is the point of
        the choice. If it is `dict` + `sorted`, this is where the sort happens,
        and it happening *here* rather than on every insert is the reason that
        choice is fast.
        """
        raise NotImplementedError("V3: iterate entries in ascending key order")
