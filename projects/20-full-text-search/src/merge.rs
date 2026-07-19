//! V4 — Segment merging & deletes.
//!
//! Every refresh writes a new segment (V2), so a busy shard drifts toward hundreds
//! of tiny segments — and a query has to consult *all* of them, so search slows as
//! the segment count climbs. **Merging** is the compaction that fights back: combine
//! many small segments into one larger immutable segment, then retire the inputs.
//! It's the same idea as LSM-tree compaction (project 22): sequential writes are
//! cheap, so you buy read speed by periodically rewriting.
//!
//! Deletes ride along. Segments are immutable, so you can't remove a document from
//! one. Instead a delete records a **tombstone** in the shard's [`LiveDocs`] overlay;
//! search skips tombstoned docs, and the space is only *actually* reclaimed when a
//! merge rewrites the segment and simply doesn't copy the dead docs across. That's
//! the write-amplification tradeoff: deletes are instant and cheap, reclamation is
//! deferred to merge time.
//!
//! Two decisions to own and document:
//!   - **When to merge** — the [`MergePolicy`]. A tiered policy merges once a shard
//!     holds more than `merge_factor` segments; force-merge collapses everything to
//!     one. Merge too eagerly and you waste I/O; too lazily and search degrades.
//!   - **What "live" means at merge time** — a merged segment contains exactly the
//!     inputs' documents that were still live, renumbered into a fresh id space.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::doc::DocId;
use crate::segment::SegmentReader;

/// The set of deleted documents in one shard — a tombstone overlay over its
/// immutable segments. Small and fully in memory; a real engine persists it beside
/// the segments so deletes survive a restart (a stretch here).
#[derive(Debug, Default)]
pub struct LiveDocs {
    deleted: HashSet<DocId>,
}

impl LiveDocs {
    pub fn new() -> Self {
        Self::default()
    }

    /// Tombstone a document. Idempotent — deleting an already-dead doc is a no-op.
    pub fn delete(&mut self, doc_id: DocId) {
        self.deleted.insert(doc_id);
    }

    /// Whether a document is still live (not tombstoned). The scorer (V3) calls this
    /// per posting to skip deleted docs.
    pub fn is_live(&self, doc_id: DocId) -> bool {
        !self.deleted.contains(&doc_id)
    }

    /// How many docs are tombstoned — the reclaimable space a merge would recover.
    pub fn deleted_count(&self) -> usize {
        self.deleted.len()
    }
}

/// Decides when a shard's segments should be merged.
#[derive(Debug, Clone, Copy)]
pub struct MergePolicy {
    /// Merge once a shard holds more than this many segments (tiered trigger).
    pub merge_factor: usize,
}

impl MergePolicy {
    pub fn new(merge_factor: usize) -> Self {
        Self { merge_factor }
    }

    /// Pick the segments to merge, or `None` if the shard is already tidy. Returns
    /// indices into `segments`.
    ///
    /// TODO(V4): implement the policy. Simplest workable rule: if
    /// `segments.len() > merge_factor`, choose a batch to combine (all of them, or
    /// the smallest `merge_factor` — merging like-sized segments keeps write
    /// amplification down). Document the rule you pick and why.
    pub fn plan(&self, segments: &[Arc<SegmentReader>]) -> Option<Vec<usize>> {
        let _ = (self.merge_factor, segments);
        todo!("V4: decide which segments (if any) to merge")
    }
}

/// Merge `inputs` into a single new segment under `dir`, dropping any document that
/// is not live per `live`, and return the new segment's path. **The core of V4.**
///
/// TODO(V4): the merge —
///   1. Stream each input's terms in sorted order and k-way merge their postings so
///      the output dictionary stays sorted (don't load everything into RAM — that
///      defeats the point of segments).
///   2. Skip tombstoned docs (`!live.is_live`); renumber the surviving docs into a
///      fresh contiguous id space for the output segment, remapping their postings.
///   3. Carry stored fields + lengths across for the survivors; recompute the
///      footer's `doc_count` / `total_length`.
///   4. Write via a [`SegmentWriter`](crate::segment::SegmentWriter), fsync, and
///      return the path. The caller swaps the new segment in for the inputs and
///      retires them once no search still holds their `Arc`.
pub fn merge(
    dir: &Path,
    seg_id: u64,
    inputs: &[Arc<SegmentReader>],
    live: &LiveDocs,
) -> std::io::Result<PathBuf> {
    let _ = (dir, seg_id, inputs, live);
    todo!("V4: k-way merge the input segments into one, dropping tombstoned docs")
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove merge + deletes —
    //   - merging N segments yields one segment whose postings equal the union of
    //     the inputs' *live* postings (nothing lost, nothing resurrected) — see
    //     `prop_merge_preserves_live_docs`;
    //   - a tombstoned doc is absent from the merged segment and from search results
    //     the instant it's deleted (before the merge even runs);
    //   - after a force-merge the shard holds exactly one segment and search results
    //     are unchanged;
    //   - LiveDocs.is_live is the only thing search consults for deletes (deleting
    //     then searching skips the doc without touching disk).
}
