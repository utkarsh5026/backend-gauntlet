"""V4 — Segment merging & deletes.

Every refresh writes a new segment (V2), so a busy shard drifts toward hundreds
of tiny ones — and a query has to consult *all* of them, so search slows as the
count climbs. **Merging** is the compaction that fights back: combine many small
segments into one larger immutable segment, then retire the inputs. It is the
same idea as LSM-tree compaction (project 22): sequential writes are cheap, so
you buy read speed by periodically rewriting.

Deletes ride along. Segments are immutable, so you cannot remove a document from
one. Instead a delete records a **tombstone** in the shard's `LiveDocs` overlay;
search skips tombstoned documents, and the space is only actually reclaimed when
a merge rewrites the segment and declines to copy the dead ones across. That is
the write-amplification trade: deletes are instant and cheap, reclamation is
deferred to merge time.

Two decisions to own and document:

*   **When to merge** — the `MergePolicy`. A tiered policy merges once a shard
    holds more than `merge_factor` segments; a force-merge collapses everything
    to one. Merge too eagerly and you waste I/O; too lazily and search degrades.
*   **What "live" means at merge time** — a merged segment contains exactly the
    inputs' still-live documents, renumbered into a fresh id space.

**The Python tool that makes the merge itself easy — and the one that makes it
wrong.** The core operation is a k-way merge of sorted term streams, and the
stdlib has it: `heapq.merge(*iterables, key=...)` consumes sorted iterables
**lazily** and yields one sorted stream, holding only one item per input in
memory. Pair it with `itertools.groupby` to collapse the runs of equal terms
coming from different segments, and the whole merge is a streaming pipeline that
never materializes the combined dictionary. That laziness is not a nicety — the
SPEC's first criterion is that merging works without loading everything into RAM,
and `sorted(chain(*dicts))` quietly violates it while passing every test on a
small corpus.

The wrong tool is `dict.update()` or `a | b` to combine per-term postings: it
*replaces* the value for a duplicate key rather than concatenating the postings
lists, so a term present in two segments silently loses one segment's documents.
That failure is invisible until the corpus is big enough for terms to span
segments — which is why the SPEC asks for a property test over generated
segments rather than a hand-written example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .doc import DocId

if TYPE_CHECKING:
    from .segment import SegmentReader

__all__ = ["LiveDocs", "MergePolicy", "merge"]


@dataclass(slots=True)
class LiveDocs:
    """The set of deleted documents in one shard — a tombstone overlay over its
    immutable segments.

    Small and fully in memory. A real engine persists it beside the segments so
    deletes survive a restart, which is the stretch on this vertical: as written,
    a restart resurrects every deleted document, and that is a contract you
    should state explicitly rather than discover.

    Fully implemented — a `set` is genuinely the right answer here and there is
    nothing to learn from re-deriving it. Finding *which* document to tombstone
    is the work, and that lives in `index.py`.
    """

    deleted: set[DocId] = field(default_factory=set[DocId])

    def delete(self, doc_id: DocId) -> None:
        """Tombstone a document. Idempotent — deleting a dead doc is a no-op."""
        self.deleted.add(doc_id)

    def is_live(self, doc_id: DocId) -> bool:
        """Whether a document is still live.

        The scorer (V3) calls this once per posting, so it is on the hottest path
        in the engine. `in` on a `set` is the reason this is affordable.
        """
        return doc_id not in self.deleted

    @property
    def deleted_count(self) -> int:
        """How many docs are tombstoned — the space a merge would reclaim."""
        return len(self.deleted)


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """Decides when a shard's segments should be merged."""

    merge_factor: int = 10
    """Merge once a shard holds more than this many segments (a tiered trigger)."""

    def plan(self, segments: list[SegmentReader]) -> list[int] | None:
        """Pick the segments to merge, or `None` if the shard is already tidy.
        Returns indices into `segments`.

        TODO(V4): implement the policy. The simplest workable rule: if there are
        more than `merge_factor` segments, choose a batch to combine — all of
        them, or the smallest `merge_factor` of them. Merging like-sized segments
        keeps write amplification down, because repeatedly merging a tiny segment
        into a huge one rewrites the huge one every time.

        Returning indices rather than the readers themselves is deliberate: the
        caller has to splice the output back into an ordered list and retire
        exactly those inputs, and positions are what it needs to do that safely.
        Whatever rule you pick, document it and why in `docs/20-design.md` — the
        SPEC grades the *deliberateness*, not the specific threshold.
        """
        raise NotImplementedError("V4: decide which segments (if any) to merge")


def merge(directory: Path, seg_id: int, inputs: list[SegmentReader], live: LiveDocs) -> Path:
    """Merge `inputs` into a single new segment under `directory`, dropping any
    document that is not live per `live`. Returns the new segment's path.
    **The core of V4.**

    A module-level function rather than a method, because a merge is a pure
    transformation of files: N readers in, one path out, no shared state. That
    also makes it trivially safe to run in a worker thread, which is where the
    shard sends it (a merge is heavy blocking I/O and must not sit on the event
    loop).

    TODO(V4): the merge —
      1. Stream each input's terms in sorted order and k-way merge them, so the
         output dictionary comes out sorted without ever holding the combined
         dictionary in memory. `heapq.merge` + `itertools.groupby` is the
         pipeline; see the module docstring for why laziness is a criterion here
         and not a preference.
      2. Skip tombstoned documents (`live.is_live` is false) and renumber the
         survivors into a fresh contiguous id space for the output, remapping
         their postings as you go. Build the old→new mapping *first*, in one pass
         over the inputs' documents, so the postings rewrite is a dict lookup.
      3. Carry the stored fields and lengths across for the survivors, and
         recompute `doc_count` / `total_length` for the footer from the survivors
         only — copying the inputs' totals is the bug that makes IDF drift after
         every merge.
      4. Write through a `SegmentWriter` and return the path, with the same fsync
         discipline `flush` uses.

    The caller swaps the new segment in for the inputs and closes the retired
    readers. Note the ordering constraint that follows: the new file must be
    fully durable *before* the old ones are dropped, or a crash mid-merge loses
    documents that were only in the inputs.
    """
    raise NotImplementedError("V4: k-way merge the input segments into one, dropping dead docs")
