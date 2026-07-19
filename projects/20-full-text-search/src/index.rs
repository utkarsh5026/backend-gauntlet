//! One shard's inverted index — the owner that composes the verticals.
//!
//! Plumbing, not a vertical: this type wires the pieces together but holds none of
//! the interesting logic itself. An [`Index`] is a single shard — an in-memory buffer
//! of not-yet-searchable documents, an ordered list of immutable on-disk segments
//! (V2), a tombstone overlay ([`LiveDocs`], V4), a shared analyzer (V1), and a scorer
//! (V3). The [`ShardedIndex`](crate::shard::ShardedIndex) owns several of these and
//! fans queries across them (V5).
//!
//! The near-real-time model, straight from Lucene/Elasticsearch:
//!   - **index** buffers an analyzed document in memory — *not yet searchable*;
//!   - **refresh** flushes the buffer into a new immutable segment — *now searchable*;
//!   - **search** consults only the on-disk segments (never the buffer);
//!   - **merge** compacts segments and reclaims tombstoned space (V4).
//! The gap between index and refresh is why search is "near-real-time": a document is
//! invisible until the next refresh. That refresh interval is a latency-vs-throughput
//! knob you tune.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use serde::Serialize;
use tokio::sync::{Mutex, RwLock};

use crate::analyzer::Analyzer;
use crate::bm25::{Bm25, Bm25Params};
use crate::doc::{AnalyzedDoc, CollectionStats, DocId, NewDocument, SearchHit, ShardId, Term};
use crate::error::AppError;
use crate::merge::{self, LiveDocs, MergePolicy};
use crate::segment::{SegmentReader, SegmentWriter, StoredDoc};

/// A document waiting in the buffer for the next refresh.
struct BufferedDoc {
    doc_id: DocId,
    analyzed: AnalyzedDoc,
    stored: StoredDoc,
}

/// A point-in-time view of one shard, for `GET /_stats`.
#[derive(Debug, Clone, Serialize)]
pub struct ShardStats {
    pub shard: ShardId,
    /// Immutable segments currently searchable.
    pub segments: usize,
    /// Documents buffered but not yet refreshed into a segment.
    pub buffered: usize,
    /// Live + tombstoned documents across this shard's segments.
    pub doc_count: u64,
    pub deleted: usize,
}

/// One shard's inverted index. Cloneably shared as `Arc<Index>` so the coordinator
/// can fan out searches concurrently (V5).
pub struct Index {
    shard_id: ShardId,
    dir: PathBuf,
    analyzer: Arc<Analyzer>,
    scorer: Bm25,
    policy: MergePolicy,
    /// Documents indexed but not yet flushed to a segment (invisible to search).
    buffer: Mutex<Vec<BufferedDoc>>,
    /// Immutable, searchable segments (newest last). Read lock-free on the hot path.
    segments: RwLock<Vec<Arc<SegmentReader>>>,
    /// Tombstone overlay (V4): which doc ids are deleted.
    live: RwLock<LiveDocs>,
    /// Next per-shard document id to assign.
    next_doc_id: AtomicU64,
    /// Next segment file id (also used for merge outputs).
    next_seg_id: AtomicU64,
}

impl Index {
    /// Open (creating if needed) the shard rooted at `dir`.
    ///
    /// Plumbing sets up the directory and starts empty. **Reloading existing segments
    /// on restart is V2 recovery** — deferred, exactly like the boring path starts
    /// with nothing on disk.
    pub fn open(
        shard_id: ShardId,
        dir: PathBuf,
        analyzer: Arc<Analyzer>,
        params: Bm25Params,
        merge_factor: usize,
    ) -> std::io::Result<Arc<Self>> {
        std::fs::create_dir_all(&dir)?;

        // TODO(V2 recovery): list this shard's `*.seg` files, `SegmentReader::open`
        // each, and seed `segments` + `next_seg_id` + `next_doc_id` from them so a
        // restart finds everything already indexed. For now we start empty.
        Ok(Arc::new(Self {
            shard_id,
            dir,
            analyzer,
            scorer: Bm25::new(params),
            policy: MergePolicy::new(merge_factor),
            buffer: Mutex::new(Vec::new()),
            segments: RwLock::new(Vec::new()),
            live: RwLock::new(LiveDocs::new()),
            next_doc_id: AtomicU64::new(0),
            next_seg_id: AtomicU64::new(0),
        }))
    }

    pub fn shard_id(&self) -> ShardId {
        self.shard_id
    }

    /// Index a document: analyze it (V1) and buffer it for the next refresh. Returns
    /// the assigned [`DocId`]. The document is **not searchable** until a refresh.
    ///
    /// The analysis is the V1 `todo!()` — this call panics there until `analyze` is
    /// built, which is the intended worklist for `POST /documents`.
    pub async fn add_document(&self, new: NewDocument) -> Result<DocId, AppError> {
        let doc_id = DocId(self.next_doc_id.fetch_add(1, Ordering::Relaxed));

        // Same analyzer as query time (V1). Panics here until `analyze` is built.
        let analyzed = self.analyzer.analyze_doc(&new.text);
        let stored = StoredDoc {
            id: new.id,
            text: new.text,
        };

        self.buffer.lock().await.push(BufferedDoc {
            doc_id,
            analyzed,
            stored,
        });

        // TODO(observability): increment `metrics::DOCS_INDEXED_TOTAL`.
        Ok(doc_id)
    }

    /// Flush the buffer into a new immutable segment, making its documents
    /// searchable. Returns how many documents were flushed (0 = nothing buffered).
    ///
    /// Wiring is done; the flush itself is V2 (`SegmentWriter`). An empty buffer is a
    /// clean no-op, which is why `POST /_refresh` works on the bare scaffold.
    pub async fn refresh(&self) -> Result<usize, AppError> {
        let drained: Vec<BufferedDoc> = {
            let mut buffer = self.buffer.lock().await;
            std::mem::take(&mut *buffer)
        };
        if drained.is_empty() {
            return Ok(0);
        }
        let count = drained.len();

        // Build one segment from the drained docs and flush it (V2). These calls are
        // the V2 `todo!()`s.
        let mut writer = SegmentWriter::new();
        for d in drained {
            writer.add(d.doc_id, &d.analyzed, d.stored);
        }
        let seg_id = self.next_seg_id.fetch_add(1, Ordering::Relaxed);
        let path = writer.flush(&self.dir, seg_id)?;
        let reader = Arc::new(SegmentReader::open(&path)?);

        self.segments.write().await.push(reader);
        // TODO(observability): set the `metrics::SEGMENTS` gauge for this shard.
        Ok(count)
    }

    /// Search this shard for the analyzed `terms`, returning its local top-`k`.
    ///
    /// Wiring is done; the scoring loop is V3 (`Bm25::search`). Search reads only the
    /// immutable segments — never the buffer — so a document not yet refreshed does
    /// not appear here.
    pub async fn search_local(&self, terms: &[Term], k: usize) -> Result<Vec<SearchHit>, AppError> {
        let segments = self.segments.read().await;
        let live = self.live.read().await;
        let stats = collection_stats(&segments[..]);
        // V3 `todo!()`: score the matching docs and keep the top-k.
        let hits = self
            .scorer
            .search(terms, &segments[..], &live, stats, self.shard_id, k);
        Ok(hits)
    }

    /// Tombstone the document with external id `external_id`; returns whether one was
    /// found. Delete is a tombstone (V4) — the space is reclaimed at the next merge.
    ///
    /// TODO(V4): resolve `external_id` → [`DocId`] (a scan of stored ids, or a
    /// dedicated id→docid map you maintain), then `self.live.write().await.delete(id)`.
    /// [`LiveDocs`] itself is built; finding the doc to tombstone is the work.
    pub async fn delete(&self, external_id: &str) -> Result<bool, AppError> {
        let _ = (external_id, &self.segments, &self.live);
        todo!("V4: find the doc for `external_id` and tombstone it in LiveDocs")
    }

    /// Force every segment in this shard to merge into one, physically dropping
    /// tombstoned docs. Returns how many segments were merged (0 = already ≤1).
    ///
    /// Wiring is done; the merge itself is V4 (`merge::merge`). A shard with 0–1
    /// segments is a clean no-op, so `POST /_forcemerge` works on the bare scaffold.
    pub async fn force_merge(&self) -> Result<usize, AppError> {
        let segs: Vec<Arc<SegmentReader>> = self.segments.read().await.clone();
        if segs.len() <= 1 {
            return Ok(0);
        }
        let seg_id = self.next_seg_id.fetch_add(1, Ordering::Relaxed);

        // V4 `todo!()`: rewrite the inputs into one segment, dropping dead docs.
        let path = {
            let live = self.live.read().await;
            merge::merge(&self.dir, seg_id, &segs, &live)?
        };
        let reader = Arc::new(SegmentReader::open(&path)?);

        *self.segments.write().await = vec![reader];
        // The tombstones are now physically applied, so the overlay resets.
        *self.live.write().await = LiveDocs::new();
        // TODO(observability): increment `metrics::MERGES_TOTAL`, update `SEGMENTS`.
        Ok(segs.len())
    }

    /// Should this shard auto-merge given its current segment count? (Coordinator can
    /// call this after a refresh.) The *decision* is V4's [`MergePolicy::plan`].
    pub async fn should_merge(&self) -> bool {
        let segs = self.segments.read().await;
        self.policy.plan(&segs[..]).is_some()
    }

    /// Snapshot this shard's stats for `GET /_stats`. Fully wired — no vertical
    /// needed, so it reports honestly on the bare scaffold.
    pub async fn stats(&self) -> ShardStats {
        let segments = self.segments.read().await;
        let doc_count = segments.iter().map(|s| s.doc_count()).sum();
        ShardStats {
            shard: self.shard_id,
            segments: segments.len(),
            buffered: self.buffer.lock().await.len(),
            doc_count,
            deleted: self.live.read().await.deleted_count(),
        }
    }
}

/// Sum a shard's live segments into collection-wide BM25 stats (corpus size + total
/// length, from which `avgdl` follows). Plumbing used by the search path.
fn collection_stats(segments: &[Arc<SegmentReader>]) -> CollectionStats {
    let mut stats = CollectionStats::default();
    for seg in segments {
        stats.doc_count += seg.doc_count();
        stats.total_length += seg.total_length();
    }
    stats
}
