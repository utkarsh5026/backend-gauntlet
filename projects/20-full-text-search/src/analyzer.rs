//! V1 — The analyzer: text → tokens.
//!
//! This is the part you'd normally get from Lucene's analysis chain. An analyzer
//! turns a blob of text into the sequence of [`Term`]s that go into (index time) or
//! query (query time) the inverted index. What it does *is* the definition of
//! "matches": if the analyzer lowercases and strips punctuation, then `"Rust!"` and
//! `"rust"` become the same term and therefore match; if it doesn't, they don't.
//!
//! The one rule that makes search work — and the trap if you get it wrong:
//! **the same analysis runs at index time and query time.** Index `"Running"` as
//! `running` but analyze the query `"running"` as `Running`, and the lookup misses
//! every time. This module is used from both paths ([`Index::add_document`] analyzes
//! documents; the coordinator analyzes queries) via one shared [`Analyzer`] — so
//! whatever you build here is symmetric by construction.
//!
//! A classic pipeline is: split into tokens (on Unicode word boundaries, not just
//! ASCII spaces) → lowercase (case-fold) → drop tokens shorter than a floor and
//! stop-words (`the`, `a`, `is`) → optionally stem (`running` → `run`). Each stage
//! is a deliberate recall-vs-precision choice you'll document.
//!
//! [`Index::add_document`]: crate::index::Index::add_document

use std::collections::HashSet;

use crate::doc::{AnalyzedDoc, Term};

/// A small, conventional English stop-word set — the highest-frequency words that
/// carry little signal and bloat postings lists. Extend or replace it; the *choice*
/// of stop list is part of the analysis contract you document.
const DEFAULT_STOPWORDS: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in", "into", "is", "it",
    "no", "not", "of", "on", "or", "such", "that", "the", "their", "then", "there", "these",
    "they", "this", "to", "was", "will", "with",
];

/// How the analyzer behaves. Sourced from env in `main`; the same config is used to
/// analyze both documents and queries.
#[derive(Debug, Clone)]
pub struct AnalyzerConfig {
    /// Case-fold tokens to lowercase so `Rust` and `rust` collapse to one term.
    pub lowercase: bool,
    /// Drop stop-words (see [`DEFAULT_STOPWORDS`]).
    pub remove_stopwords: bool,
    /// Discard tokens shorter than this many characters (0 keeps everything).
    pub min_token_len: usize,
    /// The active stop list (empty when `remove_stopwords` is false).
    pub stopwords: HashSet<String>,
}

impl AnalyzerConfig {
    /// Build a config, materializing the default stop list when stop-word removal is
    /// on. Call this from `main`, tweaking fields from env as you like.
    pub fn new(lowercase: bool, remove_stopwords: bool, min_token_len: usize) -> Self {
        let stopwords = if remove_stopwords {
            DEFAULT_STOPWORDS.iter().map(|s| s.to_string()).collect()
        } else {
            HashSet::new()
        };
        Self {
            lowercase,
            remove_stopwords,
            min_token_len,
            stopwords,
        }
    }
}

impl Default for AnalyzerConfig {
    fn default() -> Self {
        // Elasticsearch's "standard" analyzer, roughly: fold case, drop English
        // stop-words, keep everything at least 1 char. No stemmer by default.
        Self::new(true, true, 1)
    }
}

/// The analyzer. Cheap to share (`Arc<Analyzer>`) between the index path and the
/// query path so both run identical analysis.
pub struct Analyzer {
    config: AnalyzerConfig,
}

impl Analyzer {
    pub fn new(config: AnalyzerConfig) -> Self {
        Self { config }
    }

    /// Analyze `text` into an ordered stream of terms. **The core of V1.**
    ///
    /// TODO(V1): the analysis pipeline. A reasonable order:
    ///   1. Tokenize — split into candidate tokens on word boundaries. `char`
    ///      classes / `split_whitespace` is a start; Unicode segmentation is better
    ///      (`"can't"`, `"C++"`, CJK have no spaces). Decide and document.
    ///   2. Normalize — lowercase when `config.lowercase` (case-fold).
    ///   3. Filter — drop tokens shorter than `config.min_token_len` and any in
    ///      `config.stopwords`.
    ///   4. (Optional stretch) stem — collapse `running`/`ran`/`runs` → `run`.
    /// Order stays: the returned `Vec` is the token *stream*, positions implied.
    /// This method is called from BOTH indexing and querying — keep it pure.
    pub fn analyze(&self, text: &str) -> Vec<Term> {
        let _ = (&self.config, text);
        todo!("V1: tokenize + normalize + filter `text` into the term stream")
    }

    /// Analyze a document for indexing: run [`analyze`](Self::analyze), then collapse
    /// the stream into per-term frequencies and record the document length. This is
    /// the shape a segment (V2) stores and BM25 (V3) scores from.
    ///
    /// Deliberately implemented in terms of [`analyze`](Self::analyze) so index-time
    /// and query-time analysis can never drift — finish `analyze` and this works for
    /// free. (You *may* rewrite it for efficiency, but keep the two symmetric.)
    pub fn analyze_doc(&self, text: &str) -> AnalyzedDoc {
        let terms = self.analyze(text);
        let length = terms.len() as u32;

        let mut freqs: Vec<(Term, u32)> = Vec::new();
        for term in terms {
            match freqs.iter_mut().find(|(t, _)| *t == term) {
                Some((_, count)) => *count += 1,
                None => freqs.push((term, 1)),
            }
        }
        AnalyzedDoc {
            length,
            term_freqs: freqs,
        }
    }
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the analyzer —
    //   - a fixed input yields the expected term stream (case-folded, stop-words
    //     dropped, punctuation gone);
    //   - IDEMPOTENCE: re-analyzing the *joined* output of `analyze` yields the same
    //     terms (the analyzer is a fixed point on its own output) — see
    //     `prop_analyze_is_idempotent`;
    //   - a query and a document that should match produce an overlapping term set
    //     (the index-time == query-time guarantee, stated as a test).
}
