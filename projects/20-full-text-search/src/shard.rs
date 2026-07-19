//! V5 — Scatter-gather across shards.
//!
//! One inverted index on one machine tops out: the corpus outgrows memory/disk and
//! a single query thread can't touch it all fast enough. The fix is **sharding** —
//! partition the corpus across N independent indexes and query them in parallel.
//! This coordinator owns the shards, routes each document to one of them, and turns
//! a search into a **scatter-gather**: fan the query out to every shard at once,
//! then merge their partial results into one ranked answer.
//!
//! The three things that make this subtle, and the whole point of V5:
//!
//!   1. **Fan-out & merge.** A search runs on all shards *concurrently* (not in a
//!      loop) and each returns its local top-k; the coordinator merges those into a
//!      global top-k. You only need `k` from each shard — the global winner can't be
//!      outside any shard's own top-k.
//!
//!   2. **The tail dominates.** A gather is only as fast as the *slowest* shard. With
//!      enough shards, some shard is always slow, so p99 latency is a tail-latency
//!      problem — a place for per-shard timeouts / partial results, not just raw
//!      speed.
//!
//!   3. **Scores aren't globally comparable.** BM25's IDF uses *collection* stats,
//!      and each shard only knows its own. So a term's IDF differs per shard and
//!      local scores don't strictly compare. This lite engine accepts that (shards
//!      are balanced, so it's close); the real fix is a two-phase query that gathers
//!      global term stats first. Name the tradeoff in the design doc.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use serde::Serialize;

use crate::analyzer::Analyzer;
use crate::bm25::Bm25Params;
use crate::cache::QueryCache;
use crate::doc::{DocId, NewDocument, SearchHit, ShardId};
use crate::error::AppError;
use crate::index::{Index, ShardStats};

/// Startup configuration for the whole engine. Built from env in `main`.
#[derive(Debug, Clone)]
pub struct EngineConfig {
    /// Root directory; each shard gets a `shard-<n>/` subdirectory under it.
    pub index_dir: PathBuf,
    /// How many shards to partition the corpus across (fixed for the process life).
    pub shard_count: u32,
    /// BM25 parameters, shared by every shard (V3).
    pub bm25: Bm25Params,
    /// Auto-merge trigger: a shard with more than this many segments wants merging (V4).
    pub merge_factor: usize,
    /// Reject a document whose text exceeds this many bytes (security).
    pub max_doc_bytes: usize,
    /// Reject a query with more than this many analyzed terms (security).
    pub max_query_terms: usize,
    /// Query-cache capacity in entries; 0 disables it (caching horizontal).
    pub query_cache_cap: usize,
}

/// Aggregate stats across all shards, for `GET /_stats`.
#[derive(Debug, Clone, Serialize)]
pub struct EngineStats {
    pub shard_count: usize,
    pub total_docs: u64,
    pub total_segments: usize,
    pub total_buffered: usize,
    pub shards: Vec<ShardStats>,
}

/// The coordinator: owns the shards + the query cache and turns API calls into
/// scatter-gather. Cloneably shared as `Arc<ShardedIndex>` into the handlers.
pub struct ShardedIndex {
    shards: Vec<Arc<Index>>,
    /// Shared with every shard so index-time and query-time analysis are identical (V1).
    analyzer: Arc<Analyzer>,
    /// Coordinator-level cache of merged results (caching horizontal).
    cache: QueryCache,
    max_doc_bytes: usize,
    max_query_terms: usize,
    /// Round-robin cursor for routing documents that carry no client id.
    route_cursor: AtomicU64,
}

impl ShardedIndex {
    /// Open the engine: create `config.shard_count` shards under `config.index_dir`.
    pub fn open(config: EngineConfig, analyzer: Arc<Analyzer>) -> std::io::Result<Arc<Self>> {
        let mut shards = Vec::with_capacity(config.shard_count as usize);
        for i in 0..config.shard_count {
            let dir = config.index_dir.join(format!("shard-{i}"));
            shards.push(Index::open(
                i,
                dir,
                analyzer.clone(),
                config.bm25,
                config.merge_factor,
            )?);
        }
        Ok(Arc::new(Self {
            shards,
            analyzer,
            cache: QueryCache::new(config.query_cache_cap),
            max_doc_bytes: config.max_doc_bytes,
            max_query_terms: config.max_query_terms,
            route_cursor: AtomicU64::new(0),
        }))
    }

    /// Index one document. Enforces the size cap (security), routes it to a shard,
    /// and delegates. Returns `(shard, doc_id)`.
    pub async fn add_document(&self, new: NewDocument) -> Result<(ShardId, DocId), AppError> {
        if new.text.len() > self.max_doc_bytes {
            return Err(AppError::DocumentTooLarge);
        }
        let shard_idx = self.route(new.id.as_deref());
        let doc_id = self.shards[shard_idx].add_document(new).await?;
        Ok((shard_idx as ShardId, doc_id))
    }

    /// Bulk-index a batch (the `_bulk` NDJSON path). Returns how many were accepted.
    pub async fn bulk(&self, docs: Vec<NewDocument>) -> Result<usize, AppError> {
        let mut n = 0;
        for doc in docs {
            self.add_document(doc).await?;
            n += 1;
        }
        Ok(n)
    }

    /// Search all shards and return the global top-`k`. Consults the query cache
    /// (when enabled), analyzes the query once (V1, shared analyzer), fans out to the
    /// shards (V5), and caches the merged result.
    pub async fn search(&self, query: &str, k: usize) -> Result<Vec<SearchHit>, AppError> {
        if self.cache.enabled() {
            let key = Self::cache_key(query, k);
            if let Some(hits) = self.cache.get(&key) {
                // TODO(observability): count a query-cache hit.
                return Ok(hits);
            }
            // TODO(observability): count a query-cache miss.
        }

        // Analyze once at the coordinator; ship the SAME terms to every shard (V1).
        // Panics here until `analyze` is built — the worklist for `GET /search`.
        let terms = self.analyzer.analyze(query);
        if terms.len() > self.max_query_terms {
            return Err(AppError::QueryTooBroad);
        }

        let hits = self.scatter_gather(&terms, k).await?;

        if self.cache.enabled() {
            self.cache.put(Self::cache_key(query, k), hits.clone());
        }
        // TODO(observability): count a search + observe its duration for p99.
        Ok(hits)
    }

    /// Fan the analyzed query out to every shard concurrently and merge their local
    /// top-k into a global top-k. **The core of V5.**
    ///
    /// TODO(V5): the scatter-gather —
    ///   1. Kick off `shard.search_local(terms, k)` on *all* shards at once — spawn
    ///      tasks (`tokio::spawn` over `Arc<Index>`) or `join_all` a set of futures,
    ///      not a sequential `for` loop.
    ///   2. Gather the per-shard `Vec<SearchHit>` results (each already tagged with
    ///      its shard).
    ///   3. Merge into one global top-k by score (a k-way merge / bounded heap), and
    ///      truncate to `k`.
    /// Stretch: a per-shard timeout so one slow shard can't hold the whole query
    /// (return partial results), and a two-phase query that shares global term stats
    /// so BM25 scores compare across shards.
    async fn scatter_gather(
        &self,
        terms: &[crate::doc::Term],
        k: usize,
    ) -> Result<Vec<SearchHit>, AppError> {
        let _ = (&self.shards, terms, k);
        todo!("V5: fan the query out to every shard concurrently and merge their top-k")
    }

    /// Tombstone a document by its external id (V4). Routes to the id's shard.
    pub async fn delete(&self, external_id: &str) -> Result<bool, AppError> {
        let shard_idx = self.route(Some(external_id));
        self.shards[shard_idx].delete(external_id).await
    }

    /// Refresh every shard (flush buffers → segments) and invalidate the query cache.
    /// Returns the total documents made searchable.
    pub async fn refresh_all(&self) -> Result<usize, AppError> {
        let mut total = 0;
        for shard in &self.shards {
            total += shard.refresh().await?;
        }
        if self.cache.enabled() {
            self.cache.invalidate_all();
        }
        Ok(total)
    }

    /// Force-merge every shard and invalidate the query cache. Returns the total
    /// segments merged away.
    pub async fn force_merge(&self) -> Result<usize, AppError> {
        let mut total = 0;
        for shard in &self.shards {
            total += shard.force_merge().await?;
        }
        if self.cache.enabled() {
            self.cache.invalidate_all();
        }
        Ok(total)
    }

    /// Aggregate per-shard stats for `GET /_stats`.
    pub async fn stats(&self) -> EngineStats {
        let mut shards = Vec::with_capacity(self.shards.len());
        for shard in &self.shards {
            shards.push(shard.stats().await);
        }
        EngineStats {
            shard_count: shards.len(),
            total_docs: shards.iter().map(|s| s.doc_count).sum(),
            total_segments: shards.iter().map(|s| s.segments).sum(),
            total_buffered: shards.iter().map(|s| s.buffered).sum(),
            shards,
        }
    }

    /// Route a document to a shard: hash a client id for stable placement (same id →
    /// same shard, forever), or round-robin when the doc is keyless.
    fn route(&self, id: Option<&str>) -> usize {
        let n = self.shards.len().max(1);
        match id {
            Some(id) => {
                let mut hasher = DefaultHasher::new();
                id.hash(&mut hasher);
                (hasher.finish() % n as u64) as usize
            }
            None => (self.route_cursor.fetch_add(1, Ordering::Relaxed) % n as u64) as usize,
        }
    }

    /// The query-cache key: `(k, query)`. Note this keys on the *raw* query — two
    /// queries that analyze to the same terms are separate entries (a refinement is
    /// to key on the analyzed terms instead).
    fn cache_key(query: &str, k: usize) -> String {
        format!("{k}\u{1f}{query}")
    }
}
