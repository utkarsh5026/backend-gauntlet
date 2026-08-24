"""V2 — The inverted index & on-disk segments.

This is the data structure a search engine is *for*. A forward index maps
`doc → terms`; to answer "which docs contain `rust`?" you would scan every
document. An **inverted index** flips it: `term → sorted list of docs (+ term
frequency)`, so a lookup is one dictionary hit and a walk of a postings list.
Building that, and making it live on disk, is V2.

Two ideas do the heavy lifting, both borrowed from Lucene:

1.  **Immutable segments.** You never edit an index in place. Newly indexed docs
    accumulate in memory; a *refresh* flushes them into a brand-new **segment** —
    a self-contained mini-index file that is never modified again. A shard is an
    ordered pile of these (plus deletes, V4). Immutability is what makes
    concurrent search safe without locks and merging (V4) tractable.

2.  **Map the file, don't read it.** A `SegmentReader` maps its file into the
    address space and parses postings straight out of the mapped bytes. The OS
    page cache keeps hot terms resident; a cold term faults in a page. A 10 GiB
    segment answers a query with a few KiB resident.

The on-disk format is yours to design. A workable layout is
`[stored docs][postings blocks][term dictionary][footer]`, written once by
`SegmentWriter.flush` and read back through offsets recorded in the footer. Keep
the term dictionary sorted so a lookup is a binary search over the mapped bytes
rather than a linear scan.

**The Python trap that decides whether V2 is real.**
`mmap.mmap` supports slicing, and `mm[100:200]` looks like the zero-copy read you
came for. It is not: slicing an `mmap` **copies** those bytes into a fresh
`bytes` object. Slice the whole dictionary out to search it and you have loaded
the segment into the heap — precisely the thing the SPEC's fourth criterion says
you must not do, and it will pass every functional test while failing the
resident-set measurement. `memoryview(mm)[100:200]` is the zero-copy view; it
slices without copying and you materialize `bytes` only for the handful of values
you actually return. The rest of the toolkit:

*   `struct.unpack_from(fmt, buffer, offset)` reads fixed-width fields *at an
    offset* without slicing first. `struct.Struct(fmt)` compiled once at module
    level is measurably faster than the module-level functions in a loop.
*   `int.from_bytes` is the fastest way to pull a single integer out of a
    `memoryview` slice.
*   `bisect.bisect_left(seq, needle, key=...)` binary-searches without
    materializing the sequence — over a `range(term_count)` whose key function
    reads the i-th dictionary entry out of the map, that is a binary search over
    the file itself.
*   `array.array("I")` with `.frombytes()` is the natural shape for a fixed-width
    norms array (per-doc lengths) — one allocation, C-level indexing.

**Two more Python-specific facts about `mmap` you will meet:**
a map keeps the underlying file alive, so a `SegmentReader` owns a real OS
resource and must be closed — hence `close()` and the context-manager protocol
below, and why a merge (V4) has to retire readers explicitly rather than
trusting the garbage collector. And you cannot map a zero-length file at all
(`ValueError`), which is why an empty refresh must write no segment.
"""

from __future__ import annotations

import mmap
from pathlib import Path
from types import TracebackType
from typing import Self

from .doc import AnalyzedDoc, DocId, Posting, StoredDoc, Term
from .errors import CorruptSegment

__all__ = ["SEGMENT_SUFFIX", "SegmentReader", "SegmentWriter"]

SEGMENT_SUFFIX = ".seg"
"""Extension for a segment file. A shard's directory listing filtered on this is
its segment set — which is how recovery on restart finds them."""


class SegmentWriter:
    """Accumulates the documents of one refresh in memory, then writes them out
    as a single immutable segment.

    Also the vehicle a merge (V4) uses to write its output.

    Build the in-memory inverted structure incrementally in `add`; pay the sort
    and serialize cost once, in `flush`.
    """

    def __init__(self) -> None:
        # TODO(V2): the builder state. What you need, and the Python shape for it:
        #
        #   * postings: `dict[Term, list[Posting]]`. A plain dict — you do *not*
        #     need a sorted container while building. The dictionary has to be
        #     sorted **on disk**, and `sorted(self._postings)` once in `flush` is
        #     one O(n log n) pass, versus paying insertion-order maintenance on
        #     every one of the millions of `add` calls. (If you reach for a sorted
        #     structure anyway, `sortedcontainers.SortedDict` is the third-party
        #     one; the stdlib's answer is `bisect.insort` into a list.)
        #   * stored: `dict[DocId, StoredDoc]` — the fields a hit renders from.
        #   * lengths: `dict[DocId, int]` — per-doc token counts, which become the
        #     norms array BM25 (V3) reads back.
        #   * running `doc_count` and `total_length` totals for the footer.
        #
        # A `defaultdict(list)` saves you the "is this term new?" branch in `add`.
        self._empty = True

    def __len__(self) -> int:
        """How many documents are staged.

        `refresh` checks this so an empty refresh writes no segment — which is
        not just tidiness: a zero-length file cannot be mapped at all.

        TODO(V2): report from the real builder state.
        """
        return 0

    def add(self, doc_id: DocId, analyzed: AnalyzedDoc, stored: StoredDoc) -> None:
        """Add one analyzed document to the segment under construction.

        TODO(V2): for each `(term, freq)` in `analyzed.term_freqs`, append a
        `Posting(doc_id, freq)` to that term's list; record the doc's length and
        its stored fields, and fold them into the running totals.

        Postings within a term must end up sorted by `doc_id` — they will be for
        free if callers add documents in increasing id order, which the shard
        does. Rely on that deliberately and *say so*, or sort in `flush`; an
        unsorted postings list breaks the merge (V4) and any future skip list.
        """
        raise NotImplementedError("V2: accumulate this doc's postings + stored fields in memory")

    def flush(self, directory: Path, seg_id: int) -> Path:
        """Serialize the accumulated segment to a new file under `directory`,
        named by `seg_id`, and return its path. The file is immutable once this
        returns.

        TODO(V2): lay out the bytes —
          * write the stored docs, remembering each doc's offset;
          * write the per-doc lengths as one fixed-width block (`array.array`
            round-trips through `.tobytes()` / `.frombytes()`), so `doc_length`
            is an index rather than a search;
          * write each term's postings block. `[count][(doc_id, tf)…]` packed
            with `struct` is fine to start; delta-encoding the doc ids and
            varint-packing them is the stretch that makes the file small;
          * write the SORTED term dictionary: `term → (postings offset, doc_freq)`;
          * write a footer holding the dictionary offset, `doc_count` and
            `total_length` so a reader can find everything from the end.

        Put a magic number at the head and a checksum in the footer: the fifth
        criterion is that a torn file is *detected*, and you cannot detect what
        you did not record.

        Durability, and the part people skip: write to a temp name, `flush()` the
        buffer, `os.fsync(f.fileno())`, rename into place, then **fsync the
        directory** too. Without that last one the rename itself is not durable,
        and a crash leaves a directory entry pointing at nothing — which the next
        startup will happily try to map.
        """
        raise NotImplementedError("V2: serialize the inverted index to an immutable segment file")


class SegmentReader:
    """A read-only view over one immutable segment, backed by a memory map.

    Shared freely across concurrent searches: the file never changes, so there is
    nothing to synchronize. A merge (V4) retires a reader by calling `close()`
    once no search still holds it.

    Opening the file and mapping it is wiring and is done for you; parsing the
    footer — the thing that makes those bytes an index — is the V2 work.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("rb")
        try:
            if path.stat().st_size == 0:
                # An empty file cannot be mapped, and should never have been
                # written; treat it as the corruption it is rather than letting
                # `mmap` raise a bare ValueError from somewhere deeper.
                raise CorruptSegment(f"empty segment file: {path}")
            self._mmap = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            self._file.close()
            raise

        # A zero-copy window over the whole file. Slice THIS, never `self._mmap`,
        # or you are copying the segment into the heap (see the module docstring).
        self.view = memoryview(self._mmap)

        try:
            self._dict_offset, self._doc_count, self._total_length = self._read_footer()
        except BaseException:
            self.close()
            raise

    def _read_footer(self) -> tuple[int, int, int]:
        """Parse the trailing footer: `(dict_offset, doc_count, total_length)`.

        TODO(V2): read the fixed-size footer from the end of `self.view` with
        `struct.unpack_from` (a negative offset works, or index from `len`).
        Validate before you trust: check the magic bytes and the checksum, check
        that `dict_offset` actually lands inside the file, and raise
        `CorruptSegment` when any of that fails.

        This is the whole of the fifth acceptance criterion. Every read after
        this one navigates by offsets that came from here, so a footer you did
        not validate turns a byte-flip into *wrong postings* — a silently wrong
        search result — instead of an error.
        """
        raise NotImplementedError("V2: parse and validate the segment footer")

    def postings(self, term: Term) -> list[Posting] | None:
        """The sorted documents containing `term`, with term frequencies.
        `None` when the term is not in this segment. **The read-path core.**

        TODO(V2): binary-search the sorted term dictionary living in
        `self.view[self._dict_offset:]`. `bisect.bisect_left` over
        `range(term_count)` with a `key=` that decodes the i-th entry gives you a
        binary search that touches O(log n) pages and never materializes the
        dictionary. On a hit, seek to that term's postings block and decode it,
        reversing whatever encoding `flush` used.

        Read *through the memoryview*. Materialize `bytes` only for the postings
        you return — decoding the block into a `list[Posting]` is a real
        allocation and that is fine; copying the file to find the block is not.
        """
        raise NotImplementedError(
            "V2: binary-search the term dict and decode postings from the map"
        )

    def doc_length(self, doc_id: DocId) -> int | None:
        """The length in tokens of a document in this segment — BM25's per-doc
        length-normalization input (V3). `None` if `doc_id` is not here.

        TODO(V2): index the fixed-width norms block you wrote in `flush`. This is
        called once per posting per query term, so it must be an offset
        computation, never a scan. An `array.array("I")` filled once from the map
        at open time is a legitimate trade — a few MiB resident to make the
        hottest lookup in the scorer a C-level index — and if you make it, say so
        in the design doc, because it is a deliberate exception to "don't load
        the segment".
        """
        raise NotImplementedError("V2: read the stored length of `doc_id`")

    def stored(self, doc_id: DocId) -> StoredDoc | None:
        """The stored fields for a hit (external id + text), for rendering results.

        TODO(V2): read the stored-docs section at `doc_id`'s offset. Only the
        top-`k` survivors need this, so it is a cold path — decode it lazily,
        after ranking, not while scoring.
        """
        raise NotImplementedError("V2: read `doc_id`'s stored fields")

    @property
    def doc_count(self) -> int:
        """Documents in this segment — a BM25 corpus-size input (V3)."""
        return self._doc_count

    @property
    def total_length(self) -> int:
        """Sum of document lengths in this segment — for the collection `avgdl`."""
        return self._total_length

    def close(self) -> None:
        """Release the map and the file handle.

        Explicit because a memory map is an OS resource, not just memory: leaking
        readers across merges pins deleted segment files (and on Windows blocks
        the delete outright). Idempotent, so a double-retire is harmless.
        """
        view = getattr(self, "view", None)
        if view is not None:
            view.release()
            self.view = memoryview(b"")
        if not self._mmap.closed:
            self._mmap.close()
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
