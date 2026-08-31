"""V4 — SSTable: the sorted, immutable file on disk. `src/lsm_redis/sstable.py`.

When a memtable (V3) fills, it is flushed to a **Sorted String Table**: key/value
pairs written in key order and then never modified. Immutability is what makes
the whole LSM tractable — no in-place updates, no page splits, safe to read
without locks, safe to cache by block. Later writes to the same key go into
*newer* SSTables; a read reconciles across them by recency, and compaction (V6)
eventually collapses the duplicates.

The file is not one flat sorted array — that would force reading all of it to
find one key. It is structured so a point lookup touches ~one block::

    [ data block 0 ][ data block 1 ]…[ data block N ]   sorted KV pairs, ~BLOCK_SIZE each
    [ bloom filter                                  ]   V5: "is this key even here?"
    [ index: first key + offset + length per block  ]   binary-searchable, held in memory
    [ footer: offsets of the bloom + index + magic  ]   fixed size, read first

A lookup: ask the **bloom** (V5) — absent? done, skip the file entirely. Else
binary-search the in-memory **index** for the one block whose key range covers
the target, read that block through the **block cache** (V7), and search inside
it.

*Concept to internalize:* why immutability + sorted order + a sparse block index
buy O(log n) point lookups and cheap range scans on disk, and how "flush a
sorted run, never edit it" turns random writes into sequential file writes.

## Python specifics that decide whether this works under load

**Read with `os.pread`, not `seek` + `read`.** A file object has one cursor. The
moment block reads are offloaded to a thread pool — and they must be, see
`engine.py` — two threads sharing one file object will interleave a seek from
one with a read from the other and hand you bytes from the wrong block. Not an
exception: *the wrong data*, silently, under concurrency only.
`os.pread(fd, length, offset)` takes the offset as an argument and touches no
shared cursor, which makes it both correct and one syscall instead of two.

**`mmap` is the other answer, and it is a real design decision.** It hands you a
`memoryview` over the file with no read syscall and no copy, letting the OS page
cache do the caching. That is exactly what project 20 chose. It also means you
are double-caching — the page cache holds the raw bytes and your block cache
holds them again — and that the "how does the page cache interact with the block
cache" checklist item stops being rhetorical. Name your choice in
`docs/22-design.md`; both are defensible, neither is free.

**Slicing copies.** `data[a:b]` on `bytes` allocates. In a block search that
walks entries comparing keys, slicing every key out to compare it doubles your
allocation rate for nothing. `memoryview(block)[a:b]` does not copy — but note
that a `memoryview` of a `bytes` keeps the whole block alive, so do not cache
one as a value and expect the block to be freed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .block_cache import BlockCache
from .bloom import Bloom
from .memtable import Entry, Value

__all__ = ["BlockHandle", "SSTable"]

MAGIC = b"LSM22SST"
"""Fixed marker at the very end of the footer. Its job is to fail *loudly* on a
file that is not an SSTable, or is a truncated one — a half-written file whose
footer never landed must be recognizably not-an-SSTable, because the WAL still
holds those writes and recovery depends on this file being ignorable."""


@dataclass(frozen=True, slots=True)
class BlockHandle:
    """Where one data block lives, and the first key in it.

    The index is a list of these — one per block, not one per key, which is why
    it fits in memory for a file that does not. "Sparse index" is the same idea
    as project 08's offset index: land near the answer, scan a bounded distance.
    """

    first_key: bytes
    offset: int
    length: int


class SSTable:
    """A read handle to one immutable SSTable file.

    Holds the parsed index and bloom in memory (both small); data blocks stay on
    disk and are pulled through the block cache on lookup.
    """

    def __init__(self, path: Path, table_id: int, index: list[BlockHandle], bloom: Bloom) -> None:
        self.path = path
        self.id = table_id
        """Stable id, and the block cache's key namespace. Also the recency
        order: a higher id is a newer file, which is what lets the read path
        reconcile without storing a sequence number in every row."""
        self.index = index
        self.bloom = bloom

    @property
    def block_count(self) -> int:
        return len(self.index)

    @classmethod
    def create(
        cls,
        path: Path,
        table_id: int,
        entries: Iterable[tuple[bytes, Entry]],
        *,
        block_size: int,
        bloom_bits_per_key: int,
    ) -> Self:
        """Write a sorted run to a brand-new SSTable file, and return a handle.

        `entries` MUST arrive sorted ascending by key, each carrying that key's
        winning value — which is exactly what `Memtable.items_sorted()` yields
        (V3) and what a compaction merge yields (V6). Tombstones are written
        like any other entry: deletes travel into the file, and only compaction
        drops them.

        TODO(V4): accumulate entries into ~`block_size` blocks, recording a
        `BlockHandle` per block as you go; build a `Bloom` over every key (V5);
        then append the bloom, the index, and a fixed-size footer pointing at
        both, ending with `MAGIC`. `struct.pack` builds the fixed parts. Give
        each data block its own CRC — `get` has to be able to detect a
        bit-rotted block, and a checksum over the whole file cannot tell you
        *which* block went bad or let you check one without reading all of them.

        **Then make the crash safe, in this order:** write to a temp name,
        `flush` + `os.fsync` the file, `os.replace` it onto the final name
        (atomic within a filesystem), and `os.fsync` the *directory* so the name
        itself is durable. Skip the last step and a crash can leave you a file
        with contents and no name. Skip the rename and a crash mid-write leaves
        a half-file at the real name that `open` may or may not reject —
        "may or may not" being the part that makes it a data-loss bug rather
        than an error.

        This ordering is what makes V4's crash criterion true, and it is the
        same discipline `Engine.flush_memtable` needs one level up: the SSTable
        must be durable *before* the WAL segment that covers it is retired.

        Whether the on-disk row carries its `seq` is your call. It is not needed
        while recency is "higher table id wins" — but leveled compaction (V6)
        merges files from different levels, where file order stops answering the
        question. Deciding now is cheaper than adding a field to a format you
        already have files in.
        """
        raise NotImplementedError(
            "V4: write blocks + bloom (V5) + index + footer, fsync, return the handle"
        )

    @classmethod
    def open(cls, path: Path, table_id: int) -> Self:
        """Open an existing SSTable: read the footer, load the index and bloom.

        Data blocks stay on disk and are read lazily on lookup — an SSTable can
        be much larger than memory, and the whole point of the index is that you
        never need all of it at once.

        TODO(V4): read the fixed-size footer from the file tail (`os.pread` with
        a negative-from-end offset does not exist — take the size from
        `path.stat().st_size` and compute it), check `MAGIC`, then read and
        parse the index and the bloom. A bad magic, a short file, or an index
        that does not parse is `Corrupt` — raise it, never guess. This is the
        function that has to be robust against the half-written file a crash
        mid-flush leaves behind, because it runs at startup, before anything
        else works.
        """
        raise NotImplementedError("V4: parse footer -> index + bloom into memory; validate MAGIC")

    def get(self, key: bytes, cache: BlockCache) -> Value | None:
        """This table's value for `key`, or `None` if it does not have one.

        A returned `TOMBSTONE` is a **positive** answer — "deleted here" — and
        the engine's read path must stop rather than consult an older table.
        Same rule as `Memtable.get`, and the same bug if you get it wrong.

        TODO(V4): four steps, in this order, because each one exists to avoid
        the cost of the next.

        1. Ask the bloom (V5). `maybe_contains(key) is False` -> return `None`,
           having done **zero** disk I/O. This is the step V5's criterion
           measures with the block-read counter.
        2. Binary-search `self.index` for the block whose range covers `key` —
           `bisect.bisect_right` over the handles' `first_key` values, minus
           one. The off-by-one here is the classic: you want the last block
           whose `first_key <= key`, and `bisect_left` on an exact match gives
           you the block *before* the one holding it.
        3. Fetch that block through `cache` (V7). On a miss, read it with
           `os.pread`, **verify its CRC**, insert it, and use it.
        4. Search within the block for `key`.

        Step 3's CRC check is the one that is tempting to skip because it is
        never hit in testing. It is the difference between "a bit flipped on
        disk" surfacing as `Corrupt` and surfacing as a wrong answer to a
        customer six months later.
        """
        raise NotImplementedError(
            "V4: bloom-reject -> index bisect -> block via cache -> in-block search"
        )
