//! V3 — The bucket/key namespace + a crash-safe index, with prefix listing & GC.
//!
//! This maps `(bucket, key) → blob` and owns the rules that keep that mapping
//! consistent with V1's blobs across crashes and deletes. Two ideas to hold:
//!   - the keyspace is **flat** (`a/b/c.jpg` is one opaque key) — `ListObjectsV2`
//!     only *pretends* it's a tree via prefix/delimiter;
//!   - the write order is a contract: **blob durable (V2) → THEN index entry**,
//!     so a crash in between leaves a GC-able orphan blob, never a dangling key.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::error::AppError;
use crate::object::ObjectMeta;
use crate::store::Store;

/// Maps `(bucket, key)` → the blob (and metadata) backing it, and owns the
/// consistency + reclamation rules over the V1 store.
pub struct Index {
    root: PathBuf,
    store: Arc<Store>,
}

/// One page of a `ListObjectsV2` response.
pub struct Listing {
    /// The objects on this page (under the prefix, not rolled into a sub-prefix).
    pub objects: Vec<ObjectMeta>,
    /// Keys rolled up under the delimiter — the faked "subdirectories". With
    /// `delimiter=/` and `prefix=a/`, keys `a/b/c` and `a/b/d` both collapse to
    /// the single common prefix `a/b/`.
    pub common_prefixes: Vec<String>,
    /// Set when the listing was truncated at `max_keys`; pass it back to resume.
    pub next_continuation_token: Option<String>,
}

impl Index {
    /// Open (creating if needed) the index under `root/index`. The index needs a
    /// handle to the store so its GC can remove now-unreferenced blobs.
    pub fn open(root: impl AsRef<Path>, store: Arc<Store>) -> std::io::Result<Arc<Self>> {
        let root = root.as_ref().join("index");
        std::fs::create_dir_all(&root)?;
        Ok(Arc::new(Self { root, store }))
    }

    /// Create a bucket.
    pub async fn create_bucket(&self, bucket: &str) -> Result<(), AppError> {
        // TODO(V3): record the bucket. Validate the name per S3 rules (3–63
        // chars, lowercase letters/digits/hyphens, no leading/trailing hyphen) —
        // reject anything that could escape the data dir. Decide create-vs-exists
        // (AppError::BucketAlreadyExists) semantics.
        let _ = (&self.root, bucket);
        todo!("V3: create + validate a bucket")
    }

    /// Point `(bucket, key)` at a freshly-stored blob (the V2 `Stored` output).
    /// The ORDER relative to the blob write is the whole lesson.
    pub async fn put(&self, meta: ObjectMeta) -> Result<(), AppError> {
        // TODO(V3): the blob is already durable on disk — V2 committed it (V1)
        // BEFORE we got here. Now atomically record the pointer. The invariant:
        //     blob durable  →  THEN  index entry.
        // Crash in between → an unreferenced blob (garbage the GC reclaims),
        // NEVER a key pointing at a blob that isn't there. Make the index write
        // itself atomic (write-temp+rename, or append+fsync of a log). Remember
        // the OLD digest this key pointed at (on overwrite) so the GC can drop it
        // once nothing else references it.
        let _ = (&self.store, &self.root, meta);
        todo!("V3: atomically point a key at its blob, AFTER the blob is durable")
    }

    /// Look up the metadata for a key — backs `GET`/`HEAD`.
    pub async fn get(&self, bucket: &str, key: &str) -> Result<Option<ObjectMeta>, AppError> {
        // TODO(V3): return the ObjectMeta for (bucket, key), or None if absent.
        // (NoSuchBucket vs NoSuchKey is the handler's call from this Option +
        // a bucket-exists check.)
        let _ = (&self.root, bucket, key);
        todo!("V3: look up object metadata for a key")
    }

    /// Delete a key. Drops the pointer only — never the blob (dedup).
    pub async fn delete(&self, bucket: &str, key: &str) -> Result<(), AppError> {
        // TODO(V3): remove the (bucket, key) → digest entry. Do NOT delete the
        // blob inline — another key may share it. Reclamation is the GC's job.
        let _ = (&self.root, bucket, key);
        todo!("V3: delete a key (its blob stays until GC proves it unreferenced)")
    }

    /// `ListObjectsV2`: prefix/delimiter listing with pagination.
    pub async fn list(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: Option<&str>,
        continuation: Option<&str>,
        max_keys: usize,
    ) -> Result<Listing, AppError> {
        // TODO(V3): the folder illusion over a flat keyspace.
        //   - `prefix` filters to keys starting with it;
        //   - `delimiter` (usually `/`): for each matching key, look at the part
        //     AFTER the prefix; if it contains the delimiter, roll the key up
        //     into the common prefix ending at the first delimiter (a "folder")
        //     instead of listing it; otherwise list the object;
        //   - return keys in sorted order, at most `max_keys`, with a
        //     continuation token when truncated.
        let _ = (
            &self.root,
            bucket,
            prefix,
            delimiter,
            continuation,
            max_keys,
        );
        todo!("V3: prefix/delimiter listing with pagination")
    }

    /// Reclaim blobs no live key references (mark-and-sweep GC). The other half
    /// of "deleting a key doesn't delete content".
    pub async fn gc(&self) -> Result<u64, AppError> {
        // TODO(V3): mark-and-sweep.
        //   - MARK: gather every digest referenced by the live index.
        //   - SWEEP: for each blob in the store, remove it (store.remove) if no
        //     live key references it; return the count reclaimed.
        //   Mind the race: a PUT may have committed its blob (V1) but not yet
        //   written its index entry (V3) — don't reap very-recently-written
        //   blobs, or you'll delete an object out from under an in-flight upload.
        let _ = &self.store;
        todo!("V3: GC unreferenced blobs (mind the in-flight-PUT race)")
    }
}

#[cfg(test)]
mod tests {
    // TODO(V3): prove the namespace + consistency:
    //   - dedup: PUT identical bytes under two keys → one blob on disk; delete
    //     one key → the blob survives; delete the other → GC reclaims it;
    //   - list with delimiter=/ collapses keys into the right common prefixes;
    //   - pagination: max_keys + continuation token walks every key exactly once.
}
