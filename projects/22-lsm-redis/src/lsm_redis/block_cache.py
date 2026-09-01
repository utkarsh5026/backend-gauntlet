"""V7 — Block cache: a hand-built LRU over decoded SSTable blocks.
`src/lsm_redis/block_cache.py`.

An SSTable (V4) stores its data as blocks of a few KiB. A point read locates the
one block that could hold the key, reads it from disk, and searches inside it.
Under a skewed workload — a hot working set, Zipfian, like every real cache —
the *same* blocks are read over and over. Paying the read and the decode every
time is the read-amplification tax the LSM shape imposes; the block cache is how
you pay it once.

It is a bounded map from block identity to block, with **LRU** eviction: on a
hit, mark the block most-recently-used; on an insert past the byte budget, evict
least-recently-used blocks until you fit. Bound it by **bytes**, not entries:
blocks vary in size and the budget you actually have is memory.

*Concept to internalize:* how a block cache bounds an LSM's read amplification,
why LRU approximates "keep the working set" (recency ≈ reuse), the classic O(1)
LRU structure, and why bounding by bytes rather than count is what makes the
bound mean something.

## What "build it by hand" means in Python

The Rust version said "no `cargo add lru`". The Python translation is not "no
`OrderedDict`" — that would be a superstition rather than a lesson.

**`functools.lru_cache` is the forbidden shortcut**, and for concrete reasons:
it bounds by *entry count* (so it cannot satisfy the byte-bound criterion at
all), it exposes no eviction hook and no per-entry size, its statistics are a
`CacheInfo` tuple you cannot label by outcome, and it is keyed on function
arguments, so "the same block" and "the same call" have to be the same thing.
Every one of those is a criterion V7 grades.

**`collections.OrderedDict` is not a shortcut** — it *is* the hash map plus
intrusive doubly-linked list you would hand-roll, implemented in C and exposed
as `move_to_end(key)` and `popitem(last=False)`, both O(1). Reaching for it is
the same engineering decision as reaching for `HashMap` in Rust. What stays
yours is everything the vertical is actually about: the byte accounting, the
eviction loop, the too-big-to-cache case, the hit/miss counters, and the
disabled case. If you would rather build the linked list explicitly once to see
it, that is a genuinely good hour — but do it as a comparison, not as penance.

## The lock, which is a real decision here and not a formality

Rust needed a `Mutex` because threads. Whether *you* need one depends on a
choice you make in `engine.py`:

* If every cache operation happens on the event loop with no `await` between
  the read and the write, a run of plain Python statements is already
  indivisible — no other coroutine can observe a half-updated `used_bytes`. No
  lock, and adding an `asyncio.Lock` would cost a scheduler round-trip per
  block lookup to prevent a race that cannot happen.
* But disk reads must **not** run on the event loop, which means block reads
  land in a thread pool — and if the *insert* happens in that worker thread,
  two threads now touch this object and you need a real `threading.Lock`. The
  GIL is not that lock: `used_bytes -= size` is a read, a subtract and a store
  with bytecode boundaries in between.

Both designs work. The one that avoids the lock keeps the pool worker dumb — it
reads bytes and returns them, and the coroutine does the cache insert on the
loop. The one that takes the lock keeps the pool worker whole. Pick one on
purpose and write the reasoning in `docs/22-design.md`; the SPEC grades the
decision, not the choice.

If you do take a lock, `threading.Lock` is uncontended-cheap in CPython, and
sharding (N locks by `hash(key) % N`) is the standard next step when it is not —
also the standard premature optimization when contention was never measured.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import NamedTuple

__all__ = ["Block", "BlockCache", "BlockKey", "CacheStats"]

type Block = bytes
"""A block as read from disk. `bytes` rather than `bytearray` on purpose: it is
immutable, so it can be handed to any number of concurrent readers with no copy
and no risk that one of them mutates a block another is mid-search."""


class BlockKey(NamedTuple):
    """Identifies one block in the whole store: which SSTable, and where in it.

    A `NamedTuple` because it needs to be hashable, cheap to compare, and
    readable at the call site — and because tuple hashing is C-level, which
    matters for something on the hot path of every read.
    """

    sstable_id: int
    offset: int


class CacheStats(NamedTuple):
    """Hits and misses since start — the source of the hit-ratio metric the boss
    fight grades at ≥ 95%."""

    hits: int
    misses: int


class BlockCache:
    """A byte-bounded LRU over decoded SSTable blocks.

    `capacity_bytes`, `used_bytes` and `stats` are wired; `get`, `insert` and
    the eviction that keeps the bound true are V7.
    """

    def __init__(self, capacity_bytes: int) -> None:
        self.capacity_bytes = capacity_bytes
        """Byte budget. `0` disables the cache entirely: every `get` misses and
        every `insert` is a no-op, so the read path goes straight to disk. That
        has to work — it is both a V7 criterion and the state the scaffold runs
        in before you build this."""

        self._blocks: OrderedDict[BlockKey, Block] = OrderedDict()
        """Insertion-ordered map, used as recency-ordered: oldest (least
        recently used) first, so `popitem(last=False)` is the eviction."""

        self._used_bytes = 0
        self._hits = 0
        self._misses = 0

    @property
    def used_bytes(self) -> int:
        return self._used_bytes

    def stats(self) -> CacheStats:
        return CacheStats(self._hits, self._misses)

    def __len__(self) -> int:
        return len(self._blocks)

    def get(self, key: BlockKey) -> Block | None:
        """Look up a block, counting the outcome and marking a hit as
        most-recently-used.

        TODO(V7): on a hit, bump recency (`move_to_end`) and count a hit; on a
        miss, count a miss and return `None`. When `capacity_bytes == 0`, always
        miss — and note that a disabled cache still counting misses is correct
        and useful: the ratio then honestly reads 0%.

        Counting here rather than at the call site is deliberate. The hit ratio
        must describe every lookup the read path makes, and a counter the caller
        has to remember to bump is a counter that will disagree with reality on
        exactly the code path someone added in a hurry.
        """
        raise NotImplementedError(
            "V7: LRU lookup — on hit bump recency + count hit; else count miss"
        )

    def insert(self, key: BlockKey, block: Block) -> None:
        """Insert a freshly-read block, evicting LRU blocks to stay in budget.

        TODO(V7): store the block, add `len(block)` to `_used_bytes`, then evict
        from the least-recently-used end until `_used_bytes <= capacity_bytes`.
        Three cases that a naive version gets wrong, each of which is a
        criterion:

        * **Re-inserting a key already present** must not double-count its
          bytes. Subtract the old size before adding the new one, or the bound
          drifts upward until it is not a bound.
        * **A block larger than the whole capacity** should simply not be
          cached. Inserting it first and then evicting to fit empties the entire
          cache to make room for something that cannot stay — one oversized
          block would flush your whole working set.
        * **`capacity_bytes == 0`** is a no-op, not an insert followed by an
          immediate eviction.

        The byte-bound criterion is "`used_bytes` never exceeds `capacity_bytes`,
        regardless of block sizes or insert order" — *never*, including
        transiently in the middle of this method, if anything can observe it.
        Under the no-lock design nothing can, because there is no `await` here;
        under the threaded design something can, which is one more reason the
        choice in the module docstring is real.
        """
        raise NotImplementedError(
            "V7: insert + mark MRU, then evict LRU blocks until used_bytes <= capacity"
        )
