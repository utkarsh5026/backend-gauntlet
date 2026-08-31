"""V6 — Compaction: the "M" that keeps an LSM from drowning in its own writes.
`src/lsm_redis/compaction.py`.

Every flush adds another SSTable. Left alone, one key accumulates copies across
dozens of files, a read has to check all of them (**read amplification**),
deleted keys never actually free their space (**space amplification**), and
eventually flushes outrun the disk and the engine has to *stop accepting
writes* — the write stall, which is the boss. Compaction is the background
housekeeping that fixes all three: merge several sorted runs into fewer, larger
ones, keeping only the newest value per key and dropping tombstones once nothing
older survives beneath them.

The *policy* is the design choice, and it is a genuine tradeoff:

* **Size-tiered** (Cassandra) merges same-size runs. Cheap writes, worse read
  and space amplification — at the limit you hold two copies of everything
  during a merge.
* **Leveled** (LevelDB/RocksDB) keeps each level non-overlapping and ~10x the
  one above. Tighter reads and space, more write amplification — a key can be
  rewritten once per level on its way down.

Pick one, size it with `L0_COMPACTION_TRIGGER`, and justify it in
`docs/22-design.md`. "I picked leveled" is not the answer; "I picked leveled
because this workload reads more than it writes and I would rather pay write
amplification than read amplification, and here is the measurement" is.

*Concept to internalize:* the write/read/space amplification triangle — you
cannot minimize all three, and the compaction policy is exactly where you choose
which to favor. Compaction is also the only thing that makes a delete eventually
reclaim disk.

## The Python problem this vertical actually poses

"Compaction runs in the background without blocking foreground reads/writes" is
a criterion, and on CPython it is the hardest one in the project.

A merge is a long run of *Python-level* work: decode a row, compare a key,
choose a winner, re-encode, repeat, for every key in several files. That work
holds the GIL. Put it on the event loop and every connection stops being served
for the length of the merge — the boss fight's "throughput does not collapse"
criterion fails outright, and `/metrics` will show you a p99 in seconds.

Moving it to a thread with `asyncio.to_thread` helps less than you would hope:
`os.pread` and `os.write` release the GIL, so the I/O genuinely overlaps, but
the compare-and-choose loop between them does not. You get partial relief, and
the size of "partial" is a number you should measure rather than guess.

The three honest options, in rough order of how much they cost you:

1. **Thread + yield often.** Do the merge in a worker thread and keep each
   GIL-held stretch short. Simple, and enough if merges are small — which is
   what `MEMTABLE_MAX_BYTES` and the trigger are for.
2. **Chunk it on the loop.** Merge N keys, `await asyncio.sleep(0)`, repeat.
   No threads, fully cooperative, and the chunk size is a latency dial you can
   tune against the p99 target directly.
3. **A separate process.** `concurrent.futures.ProcessPoolExecutor` sidesteps
   the GIL entirely, at the cost of shipping the work across a pipe — but a
   compaction's inputs and outputs are *file paths*, not data, so the pipe
   carries almost nothing. This is the answer that scales, and it is also
   roughly what RocksDB's background threads buy in a language without a GIL.

This is exactly the kind of gap the conversion is meant to surface. Whichever
you take, the numbers and the reasoning go in `docs/22-benchmarks.md`, and if
CPython simply cannot hold the boss fight's throughput floor while compacting,
*that is the finding* — record where it topped out and why, do not scale the
target down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from .memtable import Entry
from .sstable import SSTable

if TYPE_CHECKING:
    from .engine import Engine

__all__ = ["CompactionJob", "CompactionPolicy", "compaction_loop", "merge_sorted_runs", "plan"]

logger = structlog.get_logger(__name__)


class CompactionPolicy(StrEnum):
    """Which amplification you have decided to pay. See the module docstring."""

    SIZE_TIERED = "size_tiered"
    LEVELED = "leveled"


@dataclass(frozen=True, slots=True)
class CompactionJob:
    """One unit of work the compactor decided to do: merge these input tables
    into one output at this level.

    A plan object rather than "just do it" because the decision and the merge
    fail differently and are worth testing apart — a policy bug shows up as the
    *wrong files chosen*, which a correctness test on the merge will never
    catch, and which `/metrics` will show you as an L0 count that never comes
    down.
    """

    inputs: Sequence[SSTable]
    output_level: int
    drop_tombstones: bool
    """Whether tombstones in this merge can be discarded rather than written
    through. Only true when the inputs are the *oldest* tables holding those
    keys — see `merge_sorted_runs`."""


def plan(tables: Sequence[SSTable], trigger: int, policy: CompactionPolicy) -> CompactionJob | None:
    """Decide whether to compact, and what.

    Returns `None` when there is nothing worth doing, which is the common case —
    this is called on a timer, so it must be cheap and quiet when idle.

    TODO(V6): implement the chosen policy's trigger. Size-tiered: when the
    youngest level holds `>= trigger` tables of similar size, merge that group.
    Leveled: when a level exceeds its byte budget, pick one table from it plus
    every table in the level below whose key range overlaps it.

    The `drop_tombstones` decision is made **here**, not in the merge, and it is
    the subtle part of the whole vertical: a tombstone may only be discarded
    when no table *outside* this job holds an older value for that key.
    Discard it while an older table still has the key and the deleted value
    comes back to life on the next read. When in doubt, write the tombstone
    through — it costs space, and space is the amplification you can pay later.
    """
    raise NotImplementedError(
        "V6: choose the input tables per policy, and whether tombstones may be dropped"
    )


def merge_sorted_runs(
    inputs: Sequence[SSTable],
    *,
    drop_tombstones: bool,
) -> Iterator[tuple[bytes, Entry]]:
    """Merge several sorted runs into one ascending stream, newest value wins.

    Yields exactly what `SSTable.create` consumes, so a compaction output is
    written by the same code a memtable flush uses.

    TODO(V6): a k-way merge over the inputs' entries. `heapq.merge` does the
    k-way part for you — it takes sorted iterables and a `key=`, and yields a
    single sorted stream lazily, which is what keeps a merge of files larger
    than memory inside memory. What it does *not* do is resolve duplicates:
    when the same key appears in several inputs, `heapq.merge` yields all of
    them and you must keep the newest and drop the rest. Feeding the inputs in a
    known recency order (or tagging entries with their table id) is how you know
    which is which — decide that before you write the loop, because `heapq.merge`
    is stable and that stability is what makes the ordering trick work.

    Then apply `drop_tombstones`: when it is true, a winning tombstone is
    *omitted* from the output (the key vanishes from disk and its space is
    reclaimed — this is the only place that ever happens). When false, it is
    written through like any other value.

    **Lazy on purpose.** Returning an iterator rather than a list means a merge
    of ten 64 MiB tables costs you one entry of memory, not 640 MiB, and it
    composes with the "yield to the loop every N keys" strategy in the module
    docstring — a generator is already a place where control can be handed back.
    """
    raise NotImplementedError(
        "V6: k-way merge, newest value wins, tombstones dropped only when safe"
    )


async def compaction_loop(engine: Engine, interval: float) -> None:
    """Ask the engine to compact, forever, until the task is cancelled.

    Wired — the loop shape, the error handling and the cancellation semantics
    are done. What a compaction *decides* and *does* is `plan`,
    `merge_sorted_runs`, and `Engine.run_compaction` (V6).

    Spawned only when `RUN_COMPACTION=true`, so the bare scaffold never reaches
    the unimplemented parts on a timer.

    Two things about this loop that are Python, not LSM:

    `asyncio.CancelledError` inherits from `BaseException`, not `Exception`, so
    the `except Exception` below does **not** swallow it. That is what makes
    cancellation the right shutdown mechanism: `await asyncio.sleep(...)` raises
    it, the loop unwinds, and the lifespan's `await task` returns. If you ever
    "improve" this to `except BaseException`, shutdown will hang and the reason
    will not be obvious.

    Catching and logging rather than dying is deliberate too. A compaction
    failure — a full disk, a corrupt input — must not take the compactor down
    permanently, because a dead compactor is a write stall on a delay, and it is
    silent: the server keeps answering perfectly right up until it does not.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            if await engine.run_compaction():
                logger.info("compaction ran", sstables=engine.stats().sstables)
        except NotImplementedError as exc:
            # V6 is not built yet. Say so once per tick at debug, not error:
            # this is the expected state of the scaffold, and an ERROR line per
            # second trains you to ignore the log.
            logger.debug("compaction is still a todo", detail=str(exc))
        except Exception as exc:
            logger.warning("compaction failed", error=str(exc), kind=type(exc).__name__)
