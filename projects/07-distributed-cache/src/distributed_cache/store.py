"""V1 — The per-node bounded cache with O(1) eviction.

This is the layer you'd normally get from `cachetools` (or just lean on Redis
for). Here you build it: a `dict` for O(1) lookup, plus a *second* structure that
lets you find and drop the eviction victim in O(1) too. That second structure is
the whole game — with only a map, "evict the least recently used" is an O(n) scan
on every insert, which defeats the point.

**The Python trap, and why it's the same lesson as `no cargo add lru`.**
`collections.OrderedDict` hands you LRU for free: `move_to_end(key)` on a hit and
`popitem(last=False)` to evict, both O(1). Leaning on it means you never learn
what it's doing, and it does *not* generalise — LFU can't be expressed as
"reorder on access", which is exactly why the SPEC grades a swappable policy.
Build the index yourself: a dict of nodes with explicit `prev`/`next` links for
LRU, and a frequency structure for LFU.

Scaffold state: the store is constructed and shared, but every real operation
raises. The first GET/PUT that reaches it blows up with the NotImplementedError
message — that is your worklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Entry", "EvictionPolicy", "Store"]


class EvictionPolicy(StrEnum):
    """Which victim the store drops when it's full.

    A `StrEnum` so `EvictionPolicy("lru")` parses straight from the environment
    and the value round-trips into logs and `/metrics` labels as plain text.
    """

    LRU = "lru"
    """Evict the least *recently* used entry."""

    LFU = "lfu"
    """Evict the least *frequently* used entry."""


@dataclass(slots=True)
class Entry:
    """A stored value plus its expiry.

    `slots=True` because there is one of these per cached key and the SPEC caps
    the store at 100k entries by default — the per-object `__dict__` you save is
    real memory at that count, and noticing that is part of the point.
    """

    value: bytes
    # Monotonic deadline (`time.monotonic()`-based), not a wall clock: a clock
    # adjustment must never resurrect or prematurely kill an entry.
    # `None` = never expires.
    expires_at: float | None = None


class Store:
    """The bounded local cache, shared across every request handler.

    Concurrency note you have to resolve (and document — it's a SPEC criterion):
    handlers are coroutines on one event loop, so two `get`s never interleave
    *mid-statement*, but a `get` that reads, mutates recency, and writes back is
    several statements. Anything that `await`s in the middle of that sequence, or
    any thread you offload to, can tear it. Decide whether you need a lock at
    all, and if so whether it is one lock or one per shard — and say why in
    `docs/07-design.md`.
    """

    def __init__(self, capacity: int, policy: EvictionPolicy) -> None:
        if capacity <= 0:
            raise ValueError("cache capacity must be > 0")
        self._capacity = capacity
        self._policy = policy
        # TODO(V1): your real state lives here.
        #
        #   self._entries: dict[str, Entry]  — the O(1) value lookup
        #   plus ONE of:
        #     LRU -> an intrusive doubly-linked list threaded through the keys,
        #            so a hit can splice a node to the front in O(1)
        #     LFU -> a frequency index (buckets by count, with a pointer to the
        #            current minimum) so the victim is O(1), not a min() scan
        #
        # Keep both behind a single interface — a `typing.Protocol` is the
        # idiomatic way to say "anything with these methods" without forcing a
        # base class — so LRU and LFU are swappable per the SPEC.

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def policy(self) -> EvictionPolicy:
        return self._policy

    def get(self, key: str) -> bytes | None:
        """Look up a key. Returns the value only if present **and** not expired.

        This is on the hot read path and it *mutates* bookkeeping: an LRU hit
        must move the entry to the most-recently-used position, an LFU hit must
        bump its frequency. A "read" that writes is why you can't wave this away
        as safe just because it looks like a lookup.
        """
        raise NotImplementedError(
            "V1: O(1) get that updates the eviction bookkeeping and honours TTL"
        )

    def put(self, key: str, value: bytes, ttl: float | None = None) -> None:
        """Insert or overwrite a key.

        If the store is at capacity and this is a *new* key, evict exactly one
        victim (per `policy`) before inserting. Overwriting an existing key must
        not evict anything.
        """
        raise NotImplementedError("V1: bounded put with O(1) eviction of the policy's victim")

    def remove(self, key: str) -> bool:
        """Remove a key if present; returns whether it existed."""
        raise NotImplementedError("V1: remove from both the map and the ordering index")

    def __len__(self) -> int:
        """Number of live (non-expired) entries.

        Backs the capacity-invariant test and the per-node key-count metric. An
        expired-but-not-yet-collected entry must not count.
        """
        raise NotImplementedError("V1: count of live entries")

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
