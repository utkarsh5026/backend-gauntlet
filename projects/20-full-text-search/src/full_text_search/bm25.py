"""V3 — BM25 ranking.

Matching is a set operation; *ranking* is what makes search useful. The naive
score is TF-IDF: reward a term that appears often in a document (term frequency)
and is rare across the corpus (inverse document frequency). BM25 is the
battle-tested refinement Lucene and Elasticsearch actually use, and it fixes two
real problems with raw TF-IDF:

*   **TF saturation (`k1`).** The 10th occurrence of a word should not count as
    much as the 1st. BM25 feeds tf through a curve that rises fast then flattens
    — a document is not 100x more relevant for saying "rust" 100 times.

*   **Length normalization (`b`).** A long document contains more words, so it
    racks up term frequency by sheer size. BM25 divides by
    `1 - b + b * (dl / avgdl)`, penalizing documents longer than average so a
    focused paragraph can outrank a rambling page.

The formula, per query term `t` in document `d`::

    idf(t) * ( f(t,d) * (k1 + 1) ) / ( f(t,d) + k1 * (1 - b + b * |d| / avgdl) )
    idf(t) = ln( 1 + (N - n(t) + 0.5) / (n(t) + 0.5) )

where `f(t,d)` is the term frequency, `|d|` the document length, `avgdl` the
average, `N` the corpus size and `n(t)` the number of documents containing `t`.
A document's score is the sum over the query's terms.

**The trap, and the Python shape of the answer.** With a million matching
documents you must not sort them all. `sorted(scores.items())[:k]` is O(n log n)
in the *match set*; the SPEC's fifth criterion says cost must be bounded in `k`,
not in hits. The `heapq` module is the stdlib's answer, and which function you
reach for matters:

*   `heapq.nlargest(k, iterable)` is the one-liner. It does use a bounded heap
    internally, so it is genuinely O(n log k) and it is a defensible answer — but
    it needs the iterable, so it only helps if you are streaming candidates
    rather than filling a dict first.
*   `heapq.heappushpop(heap, item)` on a `k`-element **min-heap** is the explicit
    version: push while the heap is under `k`, then push-pop, and the smallest
    score is always the one evicted. One comparison and no allocation per
    candidate past the first `k`. This is what "only the top k are materialized"
    means, and writing it once is worth more than the one-liner.

A min-heap of `(score, ...)` tuples compares the second element on a score tie,
so make sure whatever sits there is orderable — a `DocId` int is, a `SearchHit`
model is not.

**And the Python performance fact that shapes the boss fight.** This scoring loop
is the hottest pure-Python code in the project: one iteration per posting per
query term, all of it interpreted, all of it holding the GIL. It is the reason
V5's fan-out is harder in Python than in Rust (see `shard.py`), and where it tops
out is a number that belongs in `docs/20-benchmarks.md`. Attribute lookups inside
the loop (`self.params.k1`) are real cost at this volume — hoisting them into
locals before the loop is a legitimate and measurable optimization, and noticing
*why* it helps is part of learning the language.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .doc import CollectionStats, SearchHit, ShardId, Term

if TYPE_CHECKING:
    from .merge import LiveDocs
    from .segment import SegmentReader

__all__ = ["Bm25", "Bm25Params"]


@dataclass(frozen=True, slots=True)
class Bm25Params:
    """BM25's two knobs.

    The defaults are Lucene's and Elasticsearch's — a sane starting point. The
    SPEC grades the *choice*, so once you have tuned them, record the values and
    the reasoning in `docs/20-design.md`.
    """

    k1: float = 1.2
    """Term-frequency saturation. Higher rewards repeats more; 0 makes tf binary."""

    b: float = 0.75
    """Document-length normalization. 0 disables it, 1 applies it fully."""


class Bm25:
    """The scorer: parameters plus the query-execution loop over a shard's segments."""

    def __init__(self, params: Bm25Params | None = None) -> None:
        self.params = params if params is not None else Bm25Params()

    def score(
        self,
        tf: int,
        doc_len: int,
        avg_doc_len: float,
        doc_freq: int,
        doc_count: int,
    ) -> float:
        """The BM25 contribution of one term occurring in one document.

        TODO(V3): implement the formula in the module docstring from its parts —
        `tf` is `f(t,d)`, `doc_len` is `|d|`, `avg_doc_len` is `avgdl`,
        `doc_freq` is `n(t)` and `doc_count` is `N`.

        Watch the edges, because Python will not stop you at any of them:
          * `avg_doc_len == 0` (empty collection) — a `ZeroDivisionError` on the
            first search of a fresh index;
          * `doc_freq == 0` — the `+0.5` smoothing in the IDF formula exists
            exactly so this stays finite, which is worth understanding rather
            than special-casing;
          * `doc_freq > doc_count` (a stale count during a concurrent refresh)
            makes the log's argument < 1 and the IDF **negative** — a term so
            common it actively demotes documents. Lucene clamps it; decide what
            you do and write it down.

        `math.log` is the natural log the formula wants. Note `float` here, not
        `f32`: Python has one float type and it is a C double. The Rust scaffold
        deliberately used `f32` to halve the score array; you get no such choice,
        which is a small, honest example of what the language costs you.
        """
        raise NotImplementedError(
            "V3: compute the BM25 term score (idf * saturated, length-normalized tf)"
        )

    def search(
        self,
        terms: list[Term],
        segments: list[SegmentReader],
        live: LiveDocs,
        stats: CollectionStats,
        shard: ShardId,
        k: int,
    ) -> list[SearchHit]:
        """Run a query over one shard's live segments, returning its top-`k`
        hits, best first. **The query-execution core of V3.**

        Note this is a plain `def`, not `async def`, and that is deliberate: it is
        CPU-bound work with no I/O to await. Making it a coroutine would buy
        nothing and would hide that fact from you — the shard layer (V5) decides
        how to get it off the event loop.

        TODO(V3): the scoring loop —
          1. For each query `term`, gather its postings from every segment
             (`SegmentReader.postings`). The total posting count across segments
             is `n(t)`, that term's `doc_freq`, for its IDF. Compute the IDF
             **once per term**, outside the per-document loop — it does not vary
             by document, and `math.log` per posting is pure waste.
          2. Accumulate per-document scores. A `defaultdict(float)` keyed by
             document is the obvious accumulator; `dict.get(key, 0.0)` avoids the
             import and is no slower. Skip any document where `live.is_live` is
             false (V4's tombstones), and use that document's length from
             `SegmentReader.doc_length` with the shard-wide `stats`.
          3. Keep only the top `k` — the bounded heap from the module docstring,
             not a sort of the accumulator.
          4. Turn each survivor into a `SearchHit` tagged with `shard`, attaching
             stored fields via `SegmentReader.stored` — for the `k` winners only.

        The identity problem to solve first: a `DocId` is unique within a
        *segment*, and you are scoring across several. Two segments both have a
        document 0. Decide how you key the accumulator — `(segment_index, doc_id)`
        is the obvious pair — and carry enough of it through the heap to find the
        stored fields again at the end.
        """
        raise NotImplementedError(
            "V3: score matching docs across the shard's segments and keep the top-k"
        )
