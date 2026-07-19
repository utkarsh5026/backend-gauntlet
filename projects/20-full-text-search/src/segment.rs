//! V2 — The inverted index & on-disk segments.
//!
//! This is the data structure a search engine is *for*. A forward index maps
//! `doc → terms`; to answer "which docs contain `rust`?" you'd scan every document.
//! An **inverted index** flips it: `term → sorted list of docs (+ term frequency)`,
//! so a lookup is one dictionary hit and a walk of a postings list. Building that,
//! and making it live on disk, is V2.
//!
//! Two ideas do the heavy lifting, both borrowed from Lucene:
//!
//!   1. **Immutable segments.** You never edit an index in place. Newly indexed docs
//!      accumulate in memory; a *refresh* flushes them into a brand-new **segment** —
//!      a self-contained mini-index file that is never modified again. A shard is
//!      just an ordered pile of these segments (plus deletes, V4). Immutability is
//!      what makes concurrent search lock-free and merging (V4) safe.
//!
//!   2. **mmap, don't read.** A [`SegmentReader`] `mmap`s its file and parses
//!      postings straight out of the mapped bytes. The OS page cache keeps hot terms
//!      resident; a cold term faults in a page. You never `read()` the whole segment
//!      into the heap — a 10 GiB segment answers a query with a few KiB resident.
//!
//! The on-disk format is yours to design; a workable layout is
//! `[stored docs][postings blocks][term dictionary][footer with the dict offset]`,
//! written once by [`SegmentWriter::flush`] and read via offsets by
//! [`SegmentReader`]. Keep the term dictionary sorted so a lookup is a binary search
//! over the mapped bytes.

use std::path::{Path, PathBuf};

use memmap2::Mmap;

use crate::doc::{AnalyzedDoc, DocId, Posting, Term};

/// The stored fields kept for a document so a hit can be rendered without a second
/// store. Keeping the original `text` here is what lets a search return a snippet.
#[derive(Debug, Clone)]
pub struct StoredDoc {
    pub id: Option<String>,
    pub text: String,
}

/// Accumulates the documents of one refresh in memory, then writes them out as a
/// single immutable segment. Also the vehicle a merge (V4) uses to write its output.
///
/// Build the in-memory inverted structure incrementally in [`add`](Self::add); pay
/// the sort + serialize cost once in [`flush`](Self::flush).
#[derive(Default)]
pub struct SegmentWriter {
    // TODO(V2): the in-memory builder state, e.g.
    //   - a term -> Vec<Posting> map (BTreeMap keeps the dictionary sorted for you),
    //   - the stored docs (doc_id -> StoredDoc),
    //   - per-doc lengths + running totals for the segment header (doc_count,
    //     total_length) that BM25 (V3) reads back.
    _todo: (),
}

impl SegmentWriter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether anything has been added (so an empty refresh writes no segment).
    ///
    /// TODO(V2): report from the real builder state.
    pub fn is_empty(&self) -> bool {
        true
    }

    /// Add one analyzed document to the in-memory segment under construction.
    ///
    /// TODO(V2): for each `(term, freq)` in `analyzed`, append a [`Posting`]
    /// `{ doc_id, term_freq: freq }` to that term's list; keep the doc's length and
    /// stored fields. Postings within a term must end up sorted by `doc_id` (they
    /// will be if you add docs in increasing id order).
    pub fn add(&mut self, doc_id: DocId, analyzed: &AnalyzedDoc, stored: StoredDoc) {
        let _ = (doc_id, analyzed, stored);
        todo!("V2: accumulate this doc's postings + stored fields in memory")
    }

    /// Serialize the accumulated segment to a new file under `dir`, named by
    /// `seg_id`, and return its path. The file is immutable after this returns.
    ///
    /// TODO(V2): lay out the bytes —
    ///   - write stored docs and remember each doc's offset;
    ///   - write each term's postings block (delta-encode doc ids if you want the
    ///     compression; a plain `[len][ (doc_id, tf)… ]` is fine to start);
    ///   - write the SORTED term dictionary: `term -> (postings offset, doc_freq)`;
    ///   - write a footer holding the dictionary offset + `doc_count` + `total_length`
    ///     so [`SegmentReader::open`] can find everything.
    /// fsync the file (and the dir entry) before returning so a crash can't leave a
    /// half-written segment a reader will later mmap.
    pub fn flush(self, dir: &Path, seg_id: u64) -> std::io::Result<PathBuf> {
        let _ = (dir, seg_id);
        todo!("V2: serialize the inverted index to an immutable segment file")
    }
}

/// A read-only view over one immutable segment, backed by an `mmap` of its file.
///
/// Cloneably shared as `Arc<SegmentReader>`: many concurrent searches read the same
/// mapping, and a merge (V4) can retire it once no search holds the `Arc`.
pub struct SegmentReader {
    /// The whole segment file, memory-mapped. Postings and the term dictionary are
    /// parsed out of this `&[u8]` in place — never copied into the heap wholesale.
    mmap: Mmap,
    /// Live document count in this segment (from the footer) — a BM25 IDF input (V3).
    doc_count: u64,
    /// Sum of document lengths in this segment (from the footer) — for `avgdl` (V3).
    total_length: u64,
}

impl SegmentReader {
    /// Open (mmap) an existing segment file and read its footer.
    ///
    /// TODO(V2): `File::open` the path, `mmap` it, then parse the footer to recover
    /// the dictionary offset, `doc_count`, and `total_length`. Validate what you can
    /// (magic bytes / a checksum) and return [`AppError::CorruptSegment`] on a bad
    /// footer rather than trusting arbitrary bytes.
    ///
    /// [`AppError::CorruptSegment`]: crate::error::AppError::CorruptSegment
    pub fn open(path: &Path) -> std::io::Result<Self> {
        let _ = path;
        todo!("V2: mmap the segment file and parse its footer")
    }

    /// Look up a term's postings: the sorted docs that contain it (with term
    /// frequencies). `None` when the term isn't in this segment. **The read-path core.**
    ///
    /// TODO(V2): binary-search the sorted term dictionary in the mapped bytes for
    /// `term`; on a hit, decode its postings block into [`Posting`]s (reversing
    /// whatever encoding `flush` used). This reads *from the mmap* — do not load the
    /// segment into a `Vec<u8>` first.
    pub fn postings(&self, term: &Term) -> Option<Vec<Posting>> {
        let _ = (&self.mmap, term);
        todo!("V2: binary-search the term dict and decode the postings from the mmap")
    }

    /// The length (in tokens) of a document in this segment — BM25's per-doc
    /// length-normalization input (V3). `None` if `doc_id` isn't in this segment.
    ///
    /// TODO(V2): read the stored per-doc length (from the stored-docs section or a
    /// dedicated norms array).
    pub fn doc_length(&self, doc_id: DocId) -> Option<u32> {
        let _ = (&self.mmap, doc_id);
        todo!("V2: read the stored length of `doc_id`")
    }

    /// The stored fields for a hit (external id + text), for rendering results.
    ///
    /// TODO(V2): read the stored-docs section at `doc_id`'s offset.
    pub fn stored(&self, doc_id: DocId) -> Option<StoredDoc> {
        let _ = (&self.mmap, doc_id);
        todo!("V2: read `doc_id`'s stored fields")
    }

    /// Live document count in this segment (a BM25 corpus-size input).
    pub fn doc_count(&self) -> u64 {
        self.doc_count
    }

    /// Sum of document lengths in this segment (for the collection `avgdl`).
    pub fn total_length(&self) -> u64 {
        self.total_length
    }
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the segment —
    //   - flush a handful of analyzed docs, reopen the segment, and `postings(term)`
    //     returns exactly the docs that contained it, doc-id-sorted, with the right
    //     term frequencies;
    //   - a term never indexed returns `None`;
    //   - `doc_count` / `total_length` survive the flush→open round-trip (BM25 needs
    //     them);
    //   - a truncated / byte-flipped segment surfaces as CorruptSegment on open,
    //     never as wrong postings.
}
