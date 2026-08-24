"""Query cache — the caching horizontal (not a vertical, but real code to build).

Search is read-heavy and skewed: a handful of queries account for most traffic,
and re-running BM25 across every segment for the *same* query is wasted work.
This memoizes `(query, k) → results`, so a repeat is a dict hit instead of a
fan-out. It is a **coordinator-level** cache holding the final, already-merged
hits — not per-shard postings.

The correctness catch is **invalidation**. Cached results go stale the moment the
searchable set changes: a refresh adds a segment, a merge rewrites them, a delete
tombstones a document. The simple, correct policy is to drop everything on any
refresh or merge — search results are only as fresh as the last refresh anyway.
Per-segment generation stamps are the finer stretch.

**Why not `functools.lru_cache`.** It is the obvious Python reflex, it is C-speed,
and it is wrong here for two reasons worth understanding. It has no targeted
invalidation — `cache_clear()` is all-or-nothing, which happens to match the
simple policy but forecloses the stretch entirely. And it keys on the *arguments*
including `self`, which pins the coordinator alive and makes every instance share
nothing. `OrderedDict` with `move_to_end` on a hit and `popitem(last=False)` to
evict is the stdlib LRU you actually want, and it is O(1) for both.

(Contrast project 07, where an LRU was the *vertical* and reaching for a stdlib
container was the thing to avoid. Here the eviction mechanics are not what is
being graded — the invalidation and the documented policy are — so using the
right container is the correct move, not a shortcut.)

The cache is **disabled when `capacity == 0`** (the scaffold default): the engine
then never calls into it, so an unbuilt cache never sits on the search path. Set
`QUERY_CACHE_CAP > 0` once you have built the methods below.
"""

from __future__ import annotations

from .doc import SearchHit

__all__ = ["QueryCache"]


class QueryCache:
    """A bounded, invalidate-all query-result cache.

    On locking: every method here runs inside the event loop and none of them
    `await`, so no two calls can interleave and there is nothing to synchronize.
    That is a real property of the asyncio model, not luck — but it is only true
    while it stays true. The moment you add an `await` inside `get` or `put`
    (single-flight, the stretch, is exactly that), another search can run in the
    gap and you need an `asyncio.Lock`. Note which side of that line your
    implementation is on in `docs/20-design.md`.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        # TODO(caching): the store and its eviction bookkeeping.
        #
        #   self._entries: OrderedDict[str, list[SearchHit]]
        #
        # `SearchHit` is frozen, so cached lists can be handed out without
        # copying — an immutable value type is what makes a cache cheap to read.
        # Returning the list itself is still a shared mutable reference, though;
        # decide whether you hand back the list or a copy of it, and be sure the
        # caller cannot sort it in place under you.

    @property
    def enabled(self) -> bool:
        """Whether the cache is on.

        When false the engine skips it entirely — no lookups, no inserts.
        """
        return self.capacity > 0

    def get(self, key: str) -> list[SearchHit] | None:
        """Cached results for a query key, or `None` on a miss.

        Only ever called when `enabled`.

        TODO(caching): return the cached hits and bump the key's recency
        (`OrderedDict.move_to_end`). Count the outcome into
        `metrics.QUERY_CACHE_LOOKUPS` — the hit ratio is a graded number and the
        boss fight requires >= 80%, so it has to be measured, not estimated.
        """
        raise NotImplementedError("caching: return cached hits for `key`, or None")

    def put(self, key: str, hits: list[SearchHit]) -> None:
        """Insert results, evicting the least-recently-used entry past capacity.

        Only ever called when `enabled`.

        TODO(caching): insert, then evict down to `self.capacity`
        (`OrderedDict.popitem(last=False)` drops the oldest). Evict in a `while`,
        not an `if` — capacity can be lowered at runtime in a test, and an `if`
        leaves the cache permanently over budget.

        Stretch: single-flight. Under the Zipfian boss-fight mix, a cold hot key
        gets hit by hundreds of concurrent searches that all miss and all fan
        out. Parking the later ones on an `asyncio.Future` stored in the cache
        slot, so one search does the work and the rest await it, is the fix — and
        the moment this class needs a lock.
        """
        raise NotImplementedError("caching: insert `hits` under `key`, evicting LRU past capacity")

    def invalidate_all(self) -> None:
        """Drop everything — called on any refresh or merge, since those change
        what is searchable and so invalidate every cached result.

        TODO(caching): clear the store.
        """
        raise NotImplementedError("caching: clear all cached entries (called on refresh/merge)")
