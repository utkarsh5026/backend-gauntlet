//! V3 — BM25 ranking.
//!
//! Matching is a set operation; *ranking* is what makes search useful. The naive
//! score is TF-IDF: reward a term that appears often in a document (term frequency,
//! TF) and is rare across the corpus (inverse document frequency, IDF). BM25 is the
//! battle-tested refinement Lucene/Elasticsearch actually use, and it fixes two real
//! problems with raw TF-IDF:
//!
//!   - **TF saturation (`k1`).** The 10th occurrence of a word shouldn't count as
//!     much as the 1st. BM25 feeds TF through `tf / (tf + k1·…)`, a curve that rises
//!     fast then flattens — a document isn't 100× more relevant for saying "rust"
//!     100 times.
//!   - **Length normalization (`b`).** A long document contains more words, so it
//!     racks up TF by sheer size. BM25 divides by `1 - b + b·(dl/avgdl)`, penalizing
//!     documents longer than average so a focused paragraph can outrank a rambling
//!     page.
//!
//! The formula, per query term `t` in document `d`:
//! ```text
//!   idf(t) · ( f(t,d) · (k1 + 1) ) / ( f(t,d) + k1 · (1 - b + b · |d| / avgdl) )
//!   idf(t) = ln( 1 + (N - n(t) + 0.5) / (n(t) + 0.5) )
//! ```
//! where `f(t,d)` is the term frequency, `|d|` the document length, `avgdl` the
//! average, `N` the corpus size, and `n(t)` the number of docs containing `t`.
//! A document's score is the sum over the query's terms.

use std::sync::Arc;

use crate::doc::{CollectionStats, ShardId, Term};
use crate::merge::LiveDocs;
use crate::segment::SegmentReader;

/// BM25's two knobs. `k1` tunes TF saturation, `b` tunes length normalization.
#[derive(Debug, Clone, Copy)]
pub struct Bm25Params {
    pub k1: f32,
    pub b: f32,
}

impl Default for Bm25Params {
    fn default() -> Self {
        // Lucene / Elasticsearch defaults — sane starting point, then tune.
        Self { k1: 1.2, b: 0.75 }
    }
}

/// The scorer. Holds the parameters and runs a query over a shard's segments.
pub struct Bm25 {
    params: Bm25Params,
}

impl Bm25 {
    pub fn new(params: Bm25Params) -> Self {
        Self { params }
    }

    /// The BM25 contribution of a single term occurrence in one document.
    ///
    /// TODO(V3): implement the formula above from its parts:
    ///   - `tf` = term frequency `f(t,d)`, `doc_len` = `|d|`, `avg_doc_len` = `avgdl`,
    ///   - `doc_freq` = `n(t)` (docs containing the term), `doc_count` = `N`.
    /// Watch the edges: `avg_doc_len == 0` (empty collection) and `doc_freq == 0`
    /// must not divide-by-zero or produce NaN.
    pub fn score(
        &self,
        tf: u32,
        doc_len: u32,
        avg_doc_len: f64,
        doc_freq: u64,
        doc_count: u64,
    ) -> f32 {
        let _ = (&self.params, tf, doc_len, avg_doc_len, doc_freq, doc_count);
        todo!("V3: compute the BM25 term score (idf · saturated, length-normalized tf)")
    }

    /// Run a query over one shard's live segments and return its top-`k` hits, best
    /// first. **The query-execution core of V3.**
    ///
    /// TODO(V3): the scoring loop —
    ///   1. For each query `term`, gather its postings from every segment
    ///      ([`SegmentReader::postings`]); the total `doc_freq` across segments is
    ///      `n(t)` for that term's IDF.
    ///   2. Accumulate a per-document score: for each posting, skip it if the doc is
    ///      tombstoned (`!live.is_live`), else add [`score`](Self::score) using that
    ///      doc's length ([`SegmentReader::doc_length`]) and the shard-wide `stats`.
    ///   3. Keep only the top `k` by score — a bounded min-heap (`BinaryHeap` with
    ///      `Reverse`) so you never sort the whole matching set.
    ///   4. Turn each survivor into a [`SearchHit`] (tag it with `shard`, attach the
    ///      stored fields via [`SegmentReader::stored`]).
    /// Remember doc ids are per-segment-then-per-shard: disambiguate a doc that
    /// appears across segments, and tag every hit with `shard`.
    ///
    /// [`SearchHit`]: crate::doc::SearchHit
    pub fn search(
        &self,
        terms: &[Term],
        segments: &[Arc<SegmentReader>],
        live: &LiveDocs,
        stats: CollectionStats,
        shard: ShardId,
        k: usize,
    ) -> Vec<crate::doc::SearchHit> {
        let _ = (&self.params, terms, segments, live, stats, shard, k);
        todo!("V3: score matching docs across the shard's segments and keep the top-k")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the ranker —
    //   - MONOTONE IN TF: more occurrences of a query term never lowers a doc's
    //     score (saturating, but non-decreasing) — see `prop_score_monotone_in_tf`;
    //   - LENGTH PENALTY: with equal TF, the longer document scores lower;
    //   - RARER WINS: a term in fewer docs (higher IDF) contributes more than a
    //     common one at equal TF;
    //   - a known tiny corpus ranks in the order you compute by hand.
}
