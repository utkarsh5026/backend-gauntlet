"""V2 — The sparse offset index: turn a fetch-from-offset into a seek.

Every segment (V1) has a companion `.index` file next to its `.log`. It is a
**sparse** map of `(relative_offset -> byte_position)`: an entry roughly every
`interval_bytes` of log, *not* one per record. To resolve a fetch at offset K
inside a segment whose base offset is B:

  1. `lookup(K - B)` -> the largest indexed relative offset <= `K - B`, giving a
     byte position at or before K's frame;
  2. `seek()` the `.log` to that position and scan forward the handful of frames
     up to K.

Two properties make it pull its weight, and both are `Done when` criteria:

  * **Sparse:** entries are far fewer than records, so the whole index fits in
    memory cheaply.
  * **Rebuildable:** it is a *hint*, never the source of truth. Delete it and
    `rebuild_from_log` reconstructs it by scanning the segment's frames — the
    log alone is authoritative.

Positions are unsigned 32-bit and **relative to the segment start**, which is
why a segment must stay under 4 GiB (see `SEGMENT_BYTES`).

**The Python shape of this.** Two things stdlib hands you, and you should reach
for both:

  * `bisect.bisect_right` over a *sorted list of the relative offsets* is the
    binary search. Keep the offsets in their own sequence and the positions in a
    parallel one, indexed alike — `bisect` searches a plain sequence, so a list
    of `(offset, position)` tuples would make you compare tuples and allocate a
    probe key per lookup.
  * `array.array("I", ...)` is a compact, contiguous block of C `unsigned int` —
    4 bytes an entry, no per-element `PyObject`. A `list[int]` of the same length
    costs roughly 8 bytes for the pointer plus ~28 for each boxed int. At one
    entry per 4 KiB of a 64 MiB segment that difference is small; at Kafka's
    scale it is the difference between an index that fits in memory and one that
    does not. Noticing that is part of the point. `array` also gives you
    `.tobytes()` / `.frombytes()`, which *is* your file format.

Scaffold state: an `Index` is constructed and handed to every segment, but every
real operation raises. The first fetch that reaches it is your worklist.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

__all__ = ["Index", "IndexEntry"]


class IndexEntry(NamedTuple):
    """One sparse entry: "the record at this relative offset begins at this byte
    position within the segment's `.log`."

    A `NamedTuple` rather than a dataclass: it is two ints, it is read on the hot
    fetch path, and it unpacks (`offset, position = entry`) at the call site.
    """

    relative_offset: int
    """Offset relative to the segment's base offset (so it fits in 32 bits)."""

    position: int
    """Byte position of that record's frame within the `.log`."""


class Index:
    """A segment's sparse index.

    Backed by an `.index` file; the entries are *also* held in memory, sorted by
    relative offset, so the read path binary-searches RAM and only the `.log`
    itself is touched on disk.
    """

    def __init__(self, path: Path, interval_bytes: int) -> None:
        if interval_bytes <= 0:
            raise ValueError("index interval must be > 0")
        self._path = path
        self._interval_bytes = interval_bytes
        # TODO(V2): your real state lives here. Suggested shape:
        #
        #   self._offsets: array[int]   — array("I"), ascending, the search key
        #   self._positions: array[int] — array("I"), parallel to _offsets
        #   self._bytes_since_last: int — drives the "every interval_bytes"
        #                                 sparsity decision on append
        #
        # Two parallel arrays rather than one list of pairs so `bisect` can
        # search `self._offsets` directly with no probe allocation.

    @property
    def path(self) -> Path:
        return self._path

    @property
    def interval_bytes(self) -> int:
        """The documented, tunable sparsity knob. Smaller = faster seeks and more
        memory; larger = the reverse."""
        return self._interval_bytes

    @classmethod
    def create(cls, path: Path, interval_bytes: int) -> Index:
        """Create a fresh, empty index file. Plumbing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return cls(path, interval_bytes)

    @classmethod
    def open(cls, path: Path, interval_bytes: int) -> Index:
        """Open an existing index, loading its entries into memory.

        TODO(V2): read the file's `(relative_offset, position)` pairs back into
        the in-memory arrays. A missing or short file is **not** an error — leave
        it empty and let the log call `rebuild_from_log`. Plumbing returns an
        empty index for now, which is why reads still resolve (slowly) without
        one.
        """
        return cls(path, interval_bytes)

    def __len__(self) -> int:
        """Number of entries.

        Backs the sparsity criterion: this should grow like
        `segment_bytes / interval_bytes`, nowhere near the record count.
        """
        raise NotImplementedError("V2: number of sparse entries held")

    def maybe_index(self, relative_offset: int, position: int, frame_len: int) -> None:
        """Called by the append path (V1) *after* writing a frame.

        Adds an entry only once about `interval_bytes` have accrued since the
        last one — that "only sometimes" is the entire difference between a
        sparse index and a dense one.
        """
        raise NotImplementedError(
            "V2: accrue frame_len and append a sparse entry once interval_bytes have passed"
        )

    def lookup(self, relative_offset: int) -> int:
        """Byte position to start scanning from for `relative_offset`.

        The position of the largest indexed entry whose offset is `<=` the
        target, or 0 (segment start) when no entry precedes it. This is what
        bounds the forward scan to at most one `interval_bytes` of log —
        the sub-linear criterion.

        Hint: `bisect.bisect_right(self._offsets, relative_offset) - 1` is the
        index of that entry, and `-1` is exactly the "nothing precedes it" case,
        which is why the fallback is 0 rather than an error.
        """
        raise NotImplementedError("V2: binary-search the sparse index for a start position")

    def rebuild_from_log(self, log_path: Path) -> None:
        """Reconstruct the index by scanning the segment's `.log` from the start.

        This method existing, and working, is what makes the claim "the index is
        a hint, not the source of truth" true rather than aspirational.

        TODO(V2): clear the arrays, walk every frame in `log_path` tracking the
        running byte position and relative offset, re-emit a sparse entry every
        `interval_bytes`, and rewrite the file to match. Write to a temp path and
        `os.replace` it into place so a crash mid-rebuild leaves the old index,
        never a half-written one.
        """
        raise NotImplementedError("V2: rebuild the sparse index from the log alone")
