"""V5 — Scatter-gather across shards.

One inverted index on one core tops out: the corpus outgrows memory and a single
query thread cannot touch it all fast enough. The fix is **sharding** — partition
the corpus across N independent indexes and query them in parallel. This
coordinator owns the shards, routes each document to one of them, and turns a
search into a **scatter-gather**: fan the query out to every shard at once, then
merge their partial results into one ranked answer.

Three things make this subtle, and they are the whole point of V5:

1.  **Fan-out and merge.** A search runs on all shards *concurrently* and each
    returns its local top-`k`; the coordinator merges those into a global top-`k`.
    You only need `k` from each shard — the global winner cannot be outside every
    shard's own top-`k`.

2.  **The tail dominates.** A gather is only as fast as the *slowest* shard. With
    enough shards some shard is always slow, so p99 is a tail-latency problem —
    a place for per-shard timeouts and partial results, not just raw speed.

3.  **Scores are not globally comparable.** BM25's IDF uses *collection* stats and
    each shard knows only its own, so a term's IDF differs per shard and local
    scores do not strictly compare. This lite engine accepts that (balanced
    shards keep it close); the real fix is a two-phase query that gathers global
    term statistics first. Name the trade in the design doc.

**The Python problem this vertical really poses.** In Rust, `tokio::spawn` across
shards gave true parallelism and criterion 3 fell out for free. Here it does not,
and confronting that is the lesson:

*   `asyncio.gather` runs coroutines **concurrently, not in parallel**. Concurrency
    is about interleaving at `await` points; with no `await` inside, a coroutine
    runs to completion before the next one starts. `search_local` is deliberately
    a plain function (see `index.py`) doing pure CPU work — so gathering N of them
    gives you exactly the sequential loop the SPEC forbids, dressed up in async
    syntax. It will look concurrent, pass every correctness test, and show
    latency that is the *sum* of the shards.
*   `asyncio.to_thread` gets the work off the loop, which fixes responsiveness —
    but under CPython's GIL, threads running pure-Python bytecode take turns, so
    total latency is still roughly the sum. Threads only buy parallelism when the
    work releases the GIL, which page faults on a memory map do and interpreting
    bytecode does not. Where the balance lands for *your* segment reader is an
    empirical question and a benchmark number.
*   A `ProcessPoolExecutor` gives real parallelism at the cost of shipping
    arguments and results through pickle, and a memory map cannot cross that
    boundary — the workers would have to open the segments themselves, which
    means shards become processes, not objects. That is a real architecture, and
    it is what Elasticsearch does with separate JVMs per node.
*   Python 3.13 has a free-threaded build (PEP 703) with no GIL, and
    `sys._is_gil_enabled()` tells you which one you are on. That is the fourth
    option, and worth measuring if you have one.

The SPEC's criterion — latency tracks the slowest shard, not their sum — is
therefore a real experiment in Python, not a formality. Pick a vehicle, measure
it, and if it does not reach the target, `docs/20-benchmarks.md` records where it
topped out and which of the above was the cause. That gap is the finding, not a
failure.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from . import metrics
from .analyzer import Analyzer
from .bm25 import Bm25Params
from .cache import QueryCache
from .doc import DocId, NewDocument, SearchHit, ShardId, Term
from .errors import DocumentTooLarge, QueryTooBroad
from .index import Index, ShardStats

__all__ = ["EngineConfig", "EngineStats", "ShardedIndex"]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Startup configuration for the whole engine, projected from `Settings`."""

    index_dir: Path
    """Root directory; each shard gets a `shard-<n>/` subdirectory under it."""
    shard_count: int
    """How many shards to partition the corpus across (fixed for the process life)."""
    bm25: Bm25Params
    """BM25 parameters, shared by every shard (V3)."""
    merge_factor: int
    """Auto-merge trigger: a shard with more than this many segments wants merging (V4)."""
    max_doc_bytes: int
    """Reject a document whose text exceeds this many bytes (security)."""
    max_query_terms: int
    """Reject a query with more than this many analyzed terms (security)."""
    query_cache_cap: int
    """Query-cache capacity in entries; 0 disables it (caching horizontal)."""


class EngineStats(BaseModel):
    """Aggregate stats across all shards, for `GET /_stats`."""

    shard_count: int
    total_docs: int
    total_segments: int
    total_buffered: int
    shards: list[ShardStats]


class ShardedIndex:
    """The coordinator: owns the shards and the query cache, and turns API calls
    into scatter-gather."""

    def __init__(self, config: EngineConfig, analyzer: Analyzer) -> None:
        self.config = config
        # One analyzer instance shared with every shard, so index-time and
        # query-time analysis are the same code on the same config (V1).
        self.analyzer = analyzer
        self.cache = QueryCache(config.query_cache_cap)
        self.shards = [
            Index(
                shard_id=i,
                directory=config.index_dir / f"shard-{i}",
                analyzer=analyzer,
                params=config.bm25,
                merge_factor=config.merge_factor,
            )
            for i in range(config.shard_count)
        ]
        # Round-robin cursor for documents that carry no client id.
        self._route_cursor = itertools.count()

    # --- indexing ----------------------------------------------------------

    def add_document(self, new: NewDocument) -> tuple[ShardId, DocId]:
        """Index one document: enforce the size cap, route it, delegate.

        The cap is measured in **encoded bytes**, not characters. `len(str)`
        counts code points, so a document of emoji or CJK is up to four times the
        bytes its length suggests — validating on `len(new.text)` would let a
        caller past a limit that exists to bound disk and memory.
        """
        if len(new.text.encode("utf-8")) > self.config.max_doc_bytes:
            raise DocumentTooLarge()
        shard = self.route(new.id)
        return shard, self.shards[shard].add_document(new)

    def bulk(self, docs: list[NewDocument]) -> int:
        """Index a batch (the `_bulk` NDJSON path). Returns how many were accepted.

        TODO(protocols): this loops without yielding, so a large batch holds the
        event loop for its whole duration and every concurrent search waits. The
        SPEC caps the *result* set; capping or chunking the batch — and deciding
        whether a partial failure rolls back or reports per-document status the
        way Elasticsearch's `_bulk` does — is yours.
        """
        for doc in docs:
            self.add_document(doc)
        return len(docs)

    # --- search ------------------------------------------------------------

    async def search(self, query: str, k: int) -> list[SearchHit]:
        """Search all shards and return the global top-`k`.

        Consults the query cache when enabled, analyzes the query once with the
        shared analyzer (V1), fans out to the shards (V5), and caches the merged
        result.
        """
        with metrics.SEARCH_DURATION.time():
            if self.cache.enabled:
                key = self.cache_key(query, k)
                cached = self.cache.get(key)
                if cached is not None:
                    metrics.QUERY_CACHE_LOOKUPS.labels(outcome="hit").inc()
                    metrics.SEARCHES.inc()
                    return cached
                metrics.QUERY_CACHE_LOOKUPS.labels(outcome="miss").inc()

            # Analyze once at the coordinator and ship the SAME terms to every
            # shard (V1). Raises until `analyze` is built — the worklist for
            # `GET /search`.
            terms = self.analyzer.analyze(query)
            if len(terms) > self.config.max_query_terms:
                raise QueryTooBroad()

            hits = await self.scatter_gather(terms, k)

            if self.cache.enabled:
                self.cache.put(self.cache_key(query, k), hits)
            metrics.SEARCHES.inc()
            return hits

    async def scatter_gather(self, terms: list[Term], k: int) -> list[SearchHit]:
        """Fan the analyzed query out to every shard and merge their local
        top-`k` into a global top-`k`. **The core of V5.**

        TODO(V5): the scatter-gather —
          1. Run `shard.search_local(terms, k)` on *all* shards at once. Read the
             module docstring before choosing how: `asyncio.gather` over plain
             calls is the shape the SPEC asks for and, on CPython, gives you no
             parallelism at all. `asyncio.TaskGroup` (3.11+) is the modern
             structured spelling and cancels its siblings when one fails, which
             matters when you add timeouts. Whatever you pick, the benchmark has
             to show latency flat in shard count — that is the criterion.
          2. Gather the per-shard result lists (each already tagged with its shard).
          3. Merge into one global top-`k` by score. `heapq.merge` over the
             already-sorted per-shard lists with `key=` and a `reverse=True`, then
             `itertools.islice` to `k`, is a k-way merge that touches only `k`
             elements — the same bounded-work discipline as V3's top-k, one level
             up. `sorted(chain(*results))[:k]` gets the same answer and does more
             work than it needs to.

        Stretch: a per-shard timeout so one slow shard cannot hold the whole
        query. `asyncio.timeout` is the context-manager form; note that it can
        only interrupt a coroutine at an `await`, so a synchronous `search_local`
        running in the loop is *not* interruptible — another reason the choice in
        step 1 has consequences. And a two-phase query that shares global term
        statistics first, so BM25 scores compare across shards.
        """
        raise NotImplementedError(
            "V5: fan the query out to every shard concurrently and merge their top-k"
        )

    # --- admin -------------------------------------------------------------

    async def delete(self, external_id: str) -> bool:
        """Tombstone a document by its external id (V4). Routes to the id's shard."""
        found = await self.shards[self.route(external_id)].delete(external_id)
        if found and self.cache.enabled:
            self.cache.invalidate_all()
        return found

    async def refresh_all(self) -> int:
        """Refresh every shard (buffers → segments) and invalidate the query cache.

        Returns the total documents made searchable.
        """
        total = 0
        for shard in self.shards:
            total += await shard.refresh()
        if self.cache.enabled:
            self.cache.invalidate_all()
        return total

    async def force_merge(self) -> int:
        """Force-merge every shard and invalidate the query cache.

        Returns the total segments merged away.
        """
        total = 0
        for shard in self.shards:
            total += await shard.force_merge()
        if self.cache.enabled:
            self.cache.invalidate_all()
        return total

    def stats(self) -> EngineStats:
        """Aggregate per-shard stats for `GET /_stats`."""
        shards = [shard.stats() for shard in self.shards]
        return EngineStats(
            shard_count=len(shards),
            total_docs=sum(s.doc_count for s in shards),
            total_segments=sum(s.segments for s in shards),
            total_buffered=sum(s.buffered for s in shards),
            shards=shards,
        )

    def close(self) -> None:
        """Release every shard's segment maps. Called from the lifespan teardown."""
        for shard in self.shards:
            shard.close()

    # --- routing -----------------------------------------------------------

    def route(self, external_id: str | None) -> ShardId:
        """Route a document to a shard: hash a client id for stable placement, or
        round-robin when the document is keyless.

        **Do not use the builtin `hash()` here, and this is the reason.** Python
        randomizes the hash seed for `str` and `bytes` at interpreter startup
        (it is a defence against algorithmic-complexity attacks on dicts), so
        `hash("doc-1") % 3` gives a *different shard on every restart*. The
        documents stay where they were written, but every subsequent lookup and
        delete for that id goes to the wrong shard, and search still returns the
        document because it fans out to all of them — so the corruption is
        invisible until a delete silently does nothing. `PYTHONHASHSEED=0` makes
        it reproducible and is exactly the wrong fix: it disables the protection
        process-wide to paper over one misuse.

        `hashlib.blake2b` is stable across processes, versions and machines,
        which is what "the same id always lands in the same shard" requires. It
        is also faster than sha256 here, and `digest_size=8` keeps the digest to
        the eight bytes this actually needs.
        """
        n = len(self.shards)
        if external_id is None:
            return next(self._route_cursor) % n
        digest = hashlib.blake2b(external_id.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % n

    @staticmethod
    def cache_key(query: str, k: int) -> str:
        """The query-cache key: `(k, query)`, joined by a separator that cannot
        occur in either half.

        Note this keys on the *raw* query, so two queries that analyze to the
        same terms are separate entries. Keying on the analyzed terms instead
        would collapse them and raise the hit ratio the boss fight grades — a
        cheap refinement, listed here rather than done, because deciding it is
        part of documenting the cache policy.
        """
        return f"{k}\x1f{query}"
