"""One shard's inverted index — the owner that composes the verticals.

Plumbing, not a vertical: this type wires the pieces together and holds none of
the interesting logic itself. An `Index` is a single shard — an in-memory buffer
of not-yet-searchable documents, an ordered list of immutable on-disk segments
(V2), a tombstone overlay (`LiveDocs`, V4), a shared analyzer (V1) and a scorer
(V3). The `ShardedIndex` owns several of these and fans queries across them (V5).

The near-real-time model, straight from Lucene and Elasticsearch:

*   **index** buffers an analyzed document in memory — *not yet searchable*;
*   **refresh** flushes the buffer into a new immutable segment — *now searchable*;
*   **search** consults only the on-disk segments, never the buffer;
*   **merge** compacts segments and reclaims tombstoned space (V4).

The gap between index and refresh is why search is "near-real-time": a document
is invisible until the next refresh. That interval is a latency-versus-throughput
knob you tune and document.

**Which methods are `async` here is a decision, not a convention.** The rule:
a method is `async def` only if it does I/O worth yielding for. `add_document`
and `search_local` are plain functions because they are pure CPU work — making
them coroutines would add a frame, buy no concurrency, and hide from you that
they occupy the event loop for their entire duration. `refresh` and
`force_merge` are coroutines because they touch the filesystem, and they push
that work through `asyncio.to_thread` so a multi-megabyte segment write does not
stall every in-flight search. Getting this split right *is* the "no blocking call
on the event loop" checklist item; getting it wrong is invisible until load.

**How concurrent search stays lock-free.** `self._segments` is never mutated in
place — a refresh or merge builds a *new* list and rebinds the attribute. A
search that read the attribute holds the old list and can finish iterating it
safely, because the segments in it are immutable and are only closed once no
search holds them. Rebinding an attribute is a single bytecode, so a reader
either sees the old list or the new one, never a half-updated one. That is the
Python spelling of what Rust got from `Arc<RwLock<Vec<Arc<Segment>>>>` — and it
is why `list.append` on the live list would be the bug, even though it looks
equivalent.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel

from . import merge as merge_mod
from . import metrics
from .analyzer import Analyzer
from .bm25 import Bm25, Bm25Params
from .doc import (
    AnalyzedDoc,
    CollectionStats,
    DocId,
    NewDocument,
    SearchHit,
    ShardId,
    StoredDoc,
    Term,
)
from .merge import LiveDocs, MergePolicy
from .segment import SegmentReader, SegmentWriter

__all__ = ["Index", "ShardStats"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class BufferedDoc:
    """A document waiting in the buffer for the next refresh."""

    doc_id: DocId
    analyzed: AnalyzedDoc
    stored: StoredDoc


class ShardStats(BaseModel):
    """A point-in-time view of one shard, for `GET /_stats`."""

    shard: ShardId
    segments: int
    """Immutable segments currently searchable."""
    buffered: int
    """Documents indexed but not yet refreshed into a segment."""
    doc_count: int
    """Live plus tombstoned documents across this shard's segments."""
    deleted: int


class Index:
    """One shard's inverted index.

    Shared by reference across the coordinator's fan-out (V5); there is one of
    these per shard for the life of the process.
    """

    def __init__(
        self,
        shard_id: ShardId,
        directory: Path,
        analyzer: Analyzer,
        params: Bm25Params,
        merge_factor: int,
    ) -> None:
        self.shard_id = shard_id
        self.directory = directory
        self.analyzer = analyzer
        self.scorer = Bm25(params)
        self.policy = MergePolicy(merge_factor)

        directory.mkdir(parents=True, exist_ok=True)

        self._buffer: list[BufferedDoc] = []
        # Rebound, never mutated in place — see the module docstring.
        self._segments: list[SegmentReader] = []
        self._live = LiveDocs()

        # `itertools.count` is the idiomatic monotonic counter, and `next()` on
        # one is atomic in CPython — it never yields to another coroutine
        # mid-increment, so two concurrent indexes cannot get the same id. That
        # is a real guarantee of the C implementation, not of the language, and
        # it is the reason this needs no lock where Rust needed an AtomicU64.
        self._next_doc_id = itertools.count()
        self._next_seg_id = itertools.count()

        # TODO(V2 recovery): list this shard's `*.seg` files, open a
        # `SegmentReader` for each, and seed `_segments`, `_next_seg_id` and
        # `_next_doc_id` from them, so a restart finds everything already
        # indexed. Deferred on purpose — the boring path starts with nothing on
        # disk. `sorted(directory.glob("*" + SEGMENT_SUFFIX))` sorts
        # lexicographically, so zero-pad segment ids in `flush` if you want that
        # order to match creation order.
        metrics.SEGMENTS.labels(shard=str(shard_id)).set(0)

    def add_document(self, new: NewDocument) -> DocId:
        """Analyze a document (V1) and buffer it for the next refresh.

        Returns the assigned `DocId`. The document is **not searchable** until a
        refresh.

        Synchronous because analysis is CPU work with nothing to await — see the
        module docstring. That has a consequence worth measuring rather than
        assuming: a large `_bulk` request analyzes every document back-to-back
        without yielding, so searches queue behind it. Whether that shows up in
        the boss fight's p99, and what you do about it, is a benchmark finding.

        The `analyze_doc` call raises until V1 is built — that is the intended
        worklist for `POST /documents`.
        """
        doc_id: DocId = next(self._next_doc_id)
        # The same analyzer the coordinator uses on queries (V1).
        analyzed = self.analyzer.analyze_doc(new.text)
        self._buffer.append(
            BufferedDoc(
                doc_id=doc_id,
                analyzed=analyzed,
                stored=StoredDoc(id=new.id, text=new.text),
            )
        )
        metrics.DOCS_INDEXED.inc()
        return doc_id

    async def refresh(self) -> int:
        """Flush the buffer into a new immutable segment, making its documents
        searchable. Returns how many documents were flushed (0 = nothing buffered).

        Wiring is done; the flush itself is V2. An empty buffer is a clean no-op,
        which is why `POST /_refresh` works on the bare scaffold.

        Note the drain: rebinding `self._buffer` to a fresh list takes the staged
        documents in one uninterruptible step, *before* the first `await`. Doing
        it after would let another `add_document` land in the list between the
        read and the clear, and that document would be silently dropped — never
        written to a segment, never searchable, with the caller told it was
        indexed. Draining before awaiting is the whole fix.
        """
        drained, self._buffer = self._buffer, []
        if not drained:
            return 0

        writer = SegmentWriter()
        for staged in drained:
            writer.add(staged.doc_id, staged.analyzed, staged.stored)
        seg_id = next(self._next_seg_id)

        # Serializing and fsyncing a segment is blocking I/O; `to_thread` keeps
        # it off the loop so in-flight searches still make progress. The write
        # releases the GIL while it is in the kernel, which is exactly the case
        # threads are good for.
        path = await asyncio.to_thread(writer.flush, self.directory, seg_id)
        reader = await asyncio.to_thread(SegmentReader, path)

        self._segments = [*self._segments, reader]
        metrics.SEGMENTS.labels(shard=str(self.shard_id)).set(len(self._segments))
        log.info(
            "segment flushed",
            shard=self.shard_id,
            seg_id=seg_id,
            documents=len(drained),
            segments=len(self._segments),
        )
        return len(drained)

    def search_local(self, terms: list[Term], k: int) -> list[SearchHit]:
        """Score this shard's segments for `terms` and return its local top-`k`.

        Wiring is done; the scoring loop is V3. Search reads only the immutable
        segments — never the buffer — so a document not yet refreshed does not
        appear here.

        Synchronous on purpose. This is the CPU-bound core of a search, and
        leaving it a plain function forces the coordinator (V5) to make an
        explicit decision about how to run several of these at once, rather than
        letting `async def` imply a parallelism the event loop does not provide.
        """
        # Snapshot the list reference once: a refresh may rebind it mid-search,
        # and a query should see one consistent view of the index.
        segments = self._segments
        return self.scorer.search(
            terms, segments, self._live, self._collection_stats(segments), self.shard_id, k
        )

    async def delete(self, external_id: str) -> bool:
        """Tombstone the document with external id `external_id`; returns whether
        one was found. The space is reclaimed at the next merge (V4).

        TODO(V4): resolve `external_id` → `DocId`, then `self._live.delete(id)`.
        `LiveDocs` is built; *finding* the document is the work, and the shape of
        the answer is a design decision the SPEC grades:

          * scanning every segment's stored ids is O(corpus) per delete, and it
            reads from disk — which is why this method is `async` and why a scan
            belongs in `asyncio.to_thread`;
          * an in-memory `dict[str, DocId]` maintained at index time makes it
            O(1), at the cost of memory proportional to the corpus and a rebuild
            on restart.

        Pick one and write down which, and what it costs.
        """
        raise NotImplementedError("V4: find the doc for `external_id` and tombstone it in LiveDocs")

    async def force_merge(self) -> int:
        """Merge every segment in this shard into one, physically dropping
        tombstoned documents. Returns how many segments were merged (0 = already
        at most one).

        Wiring is done; the merge itself is V4. A shard with 0-1 segments is a
        clean no-op, so `POST /_forcemerge` works on the bare scaffold.
        """
        inputs = self._segments
        if len(inputs) <= 1:
            return 0
        seg_id = next(self._next_seg_id)

        path = await asyncio.to_thread(merge_mod.merge, self.directory, seg_id, inputs, self._live)
        reader = await asyncio.to_thread(SegmentReader, path)

        # Swap first, retire second: a search that started before this line keeps
        # its own reference to `inputs` and must be allowed to finish reading
        # them. Closing a map out from under a running search would segfault the
        # interpreter, not raise.
        self._segments = [reader]
        # The tombstones are now physically applied, so the overlay resets.
        self._live = LiveDocs()

        for retired in inputs:
            retired.close()
            retired.path.unlink(missing_ok=True)

        metrics.MERGES.inc()
        metrics.SEGMENTS.labels(shard=str(self.shard_id)).set(1)
        log.info("segments merged", shard=self.shard_id, merged=len(inputs), seg_id=seg_id)
        return len(inputs)

    def should_merge(self) -> bool:
        """Whether this shard's segment count warrants a merge.

        The *decision* is V4's `MergePolicy.plan`; the coordinator can call this
        after a refresh to drive auto-merging.
        """
        return self.policy.plan(self._segments) is not None

    def stats(self) -> ShardStats:
        """Snapshot this shard for `GET /_stats`.

        Fully wired, so it reports honestly on the bare scaffold — which makes it
        the one endpoint that can show you a refresh actually created a segment.
        """
        segments = self._segments
        return ShardStats(
            shard=self.shard_id,
            segments=len(segments),
            buffered=len(self._buffer),
            doc_count=sum(s.doc_count for s in segments),
            deleted=self._live.deleted_count,
        )

    def close(self) -> None:
        """Release every segment's map. Called on shutdown.

        Buffered documents are dropped here, not flushed — see the graceful
        shutdown TODO in `main.py`, where that contract is yours to decide.
        """
        for reader in self._segments:
            reader.close()
        self._segments = []

    @staticmethod
    def _collection_stats(segments: list[SegmentReader]) -> CollectionStats:
        """Sum a shard's segments into the corpus-size and total-length inputs
        BM25 needs (V3), from which `avgdl` follows."""
        return CollectionStats(
            doc_count=sum(s.doc_count for s in segments),
            total_length=sum(s.total_length for s in segments),
        )
