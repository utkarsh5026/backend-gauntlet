//! Shared vocabulary — the plain data every layer speaks.
//!
//! Plumbing, not a vertical: these are the types that flow between the analyzer
//! (V1), the on-disk segments (V2), the scorer (V3), and the API. Defining them
//! once here keeps the module boundaries honest.

use serde::{Deserialize, Serialize};

/// Which shard (V5) a document lives in. A search fans out to every shard and each
/// hit is tagged with the shard it came from — because internal doc ids are only
/// unique *within* a shard.
pub type ShardId = u32;

/// A document's internal id, assigned monotonically **by one shard** as it indexes.
/// It is what appears in a postings list. It is *not* globally unique — two shards
/// each start at 0 — so a hit always carries its [`ShardId`] alongside.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
pub struct DocId(pub u64);

/// A single analyzed token — the output of the analyzer (V1) and the key of the
/// inverted index (V2). The same [`Term`] is produced at index time and query time,
/// which is the whole reason a search matches.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Term(pub String);

/// A document as handed to the engine to index. The internal [`DocId`] is assigned
/// on index and returned; the optional `id` is the *client's* external id (like
/// Elasticsearch's `_id`) — supplying it makes the doc addressable for delete.
#[derive(Debug, Clone, Deserialize)]
pub struct NewDocument {
    /// Optional client-chosen id. When present it decides the doc's shard (stable
    /// routing) and lets `DELETE /documents/{id}` find it later.
    pub id: Option<String>,
    /// The text to index. For this lite engine a document is one text field;
    /// multi-field docs are a stretch.
    pub text: String,
}

/// The result of analyzing one document for indexing: its length in tokens plus the
/// per-term frequencies. This is exactly what a segment needs to build its postings
/// (V2) and what BM25 needs to score (V3, via the term frequency + doc length).
#[derive(Debug, Clone)]
pub struct AnalyzedDoc {
    /// Number of tokens the analyzer emitted (before de-duplication) — the document
    /// length BM25's length-normalization term (`b`) uses.
    pub length: u32,
    /// Each distinct term and how many times it occurred in this document.
    pub term_freqs: Vec<(Term, u32)>,
}

/// One entry in a postings list: a document that contains a term, and how many
/// times. Read out of a segment (V2) and consumed by the scorer (V3).
#[derive(Debug, Clone, Copy)]
pub struct Posting {
    pub doc_id: DocId,
    pub term_freq: u32,
}

/// Collection-wide statistics BM25 needs (V3): the corpus size (for IDF) and the
/// average document length (for length normalization). Summed across a shard's live
/// segments at query time.
#[derive(Debug, Clone, Copy, Default)]
pub struct CollectionStats {
    /// Number of live documents in the collection.
    pub doc_count: u64,
    /// Sum of every live document's length, in tokens.
    pub total_length: u64,
}

impl CollectionStats {
    /// Mean document length — the `avgdl` in the BM25 formula. Zero for an empty
    /// collection (the scorer must treat that as "nothing to rank").
    pub fn avg_doc_len(&self) -> f64 {
        if self.doc_count == 0 {
            0.0
        } else {
            self.total_length as f64 / self.doc_count as f64
        }
    }
}

/// A ranked search result. `score` is the BM25 relevance; `id`/`text` are the stored
/// fields, present when the segment kept them.
#[derive(Debug, Clone, Serialize)]
pub struct SearchHit {
    /// The shard this hit came from — needed because [`DocId`] is only shard-local.
    pub shard: ShardId,
    pub doc_id: DocId,
    pub score: f32,
    /// The client's external id, if the document was indexed with one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// The stored text, for rendering a snippet. Optional so a segment can choose
    /// not to store it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
}
