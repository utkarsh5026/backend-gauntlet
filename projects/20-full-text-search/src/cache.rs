//! Query cache — the caching horizontal (not a vertical, but real code to build).
//!
//! Search is read-heavy and skewed: a handful of queries account for most traffic,
//! and re-running BM25 across every segment for the *same* query is wasted work. The
//! query cache memoizes `(analyzed query, k) → results` so a repeat is a map hit
//! instead of a fan-out. It's a **coordinator-level** cache: it stores the final,
//! already-merged hits from [`ShardedIndex::search`](crate::shard::ShardedIndex::search),
//! not per-shard postings.
//!
//! The correctness catch is **invalidation**. Cached results go stale the moment the
//! searchable set changes — a refresh adds a segment, a merge rewrites them, a delete
//! tombstones a doc. The simple, correct policy: blow the whole cache away on any
//! refresh/merge (search results are only as fresh as the last refresh anyway). A
//! finer policy (per-segment generation stamps) is a stretch.
//!
//! The cache is **disabled when `cap == 0`** (the scaffold default) — the engine
//! then never calls into it, so it doesn't block building the verticals. Set
//! `QUERY_CACHE_CAP > 0` and implement the methods below to turn it on.

use crate::doc::SearchHit;

/// A bounded, invalidate-all query-result cache.
pub struct QueryCache {
    /// Capacity in entries. `0` disables the cache entirely (see [`enabled`](Self::enabled)).
    cap: usize,
    // TODO(caching): the store + eviction state, e.g. a `Mutex<{ HashMap<String,
    // Vec<SearchHit>>, order: VecDeque<String> }>` for a simple LRU. Whatever you
    // pick must be safe to share behind `&self` across concurrent searches.
    inner: std::sync::Mutex<CacheInner>,
}

#[derive(Default)]
struct CacheInner {
    // TODO(caching): fill in — the entries map and the eviction bookkeeping.
    _todo: (),
}

impl QueryCache {
    pub fn new(cap: usize) -> Self {
        Self {
            cap,
            inner: std::sync::Mutex::new(CacheInner::default()),
        }
    }

    /// Whether the cache is on. When `false`, the engine skips it entirely — no
    /// lookups, no inserts — so an unbuilt cache never sits on the search path.
    pub fn enabled(&self) -> bool {
        self.cap > 0
    }

    /// Look up cached results for a query key. Only ever called when
    /// [`enabled`](Self::enabled).
    ///
    /// TODO(caching): return a clone of the cached hits on a hit (and bump its
    /// recency for LRU); `None` on a miss.
    pub fn get(&self, key: &str) -> Option<Vec<SearchHit>> {
        let _ = (&self.inner, self.cap, key);
        todo!("caching: return cached hits for `key`, or None")
    }

    /// Insert results for a query key, evicting the least-recently-used entry if the
    /// cache is at `cap`. Only ever called when [`enabled`](Self::enabled).
    ///
    /// TODO(caching): insert, then evict down to `cap`. (Stretch: single-flight —
    /// coalesce concurrent misses on the same key so one search does the work.)
    pub fn put(&self, key: String, hits: Vec<SearchHit>) {
        let _ = (&self.inner, self.cap, key, hits);
        todo!("caching: insert `hits` under `key`, evicting LRU past `cap`")
    }

    /// Drop everything — called on any refresh or merge, since those change what is
    /// searchable and thus invalidate every cached result.
    ///
    /// TODO(caching): clear the store.
    pub fn invalidate_all(&self) {
        let _ = &self.inner;
        todo!("caching: clear all cached entries (called on refresh/merge)")
    }
}
