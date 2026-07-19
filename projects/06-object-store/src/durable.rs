//! Crash-safe publish, in one place: the temp→fsync→rename→fsync-dir dance.
//!
//! Every durable write in this store obeys the same contract: a final path must
//! only ever appear once its bytes are fully on disk, and the *rename that makes
//! it appear* must itself survive a crash. That takes four steps in a fixed
//! order — and getting any one wrong is a silent data-loss bug, not a test
//! failure. So it lives here once instead of being re-derived per call site.
//!
//! Entry points, depending on where the bytes are and who owns the temp:
//!   - [`publish_temp`] — the temp file already exists (a blob streamed in, or a
//!     cold blob just compressed). Publish it under `dest`.
//!   - [`atomic_write`] / [`atomic_write_json`] — bytes are in memory; the caller
//!     chooses the staging path (e.g. the index's per-bucket `tmp/` so GC can
//!     see in-flight digests), then this writes + publishes.
//!   - [`atomic_write_sibling`] — same, but picks a sibling temp under `dest`'s
//!     parent for you (same directory ⇒ same filesystem).
//!
//! **Same-filesystem invariant:** `rename` is only atomic *within* one
//! filesystem. Callers of [`publish_temp`] / [`atomic_write`] must pass a temp
//! on the same filesystem as `dest` (the per-bucket `tmp/` dirs already are;
//! [`atomic_write_sibling`] guarantees it by construction).

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tokio::io::AsyncWriteExt;

use crate::error::AppError;
use crate::store::TempEntry;

/// Durably publish an already-written temp file at `dest`.
///
/// The dance, in order — reorder nothing:
///   1. `fsync(temp)` — the bytes are durable before anyone can see the name.
///   2. `create_dir_all(dest.parent())` — the destination tree exists.
///   3. `rename(temp, dest)` — atomic within a filesystem, so `dest` flips from
///      "absent" to "complete" with no in-between.
///   4. `fsync(dest.parent())` — the *directory entry* (the rename itself) is
///      durable, so a crash can't rewind `dest` to its old contents.
///
/// Cleanup of `temp` on the error path is the **caller's** job (wrap it in a
/// [`TempEntry`](crate::store::TempEntry) guard), since only the caller knows
/// which staging area it came from.
///
/// # Panics
///
/// Panics if `dest` has no parent directory.
pub async fn publish_temp(temp: &Path, dest: &Path) -> Result<(), AppError> {
    tokio::fs::File::open(temp).await?.sync_all().await?;
    let parent = dest.parent().expect("dest has a parent directory");
    tokio::fs::create_dir_all(parent).await?;
    tokio::fs::rename(temp, dest).await?;
    tokio::fs::File::open(parent).await?.sync_all().await?;
    Ok(())
}

/// Write `bytes` to an existing staging path `temp`, then durably publish at `dest`.
///
/// The caller chooses `temp` (sibling of `dest`, a per-bucket `tmp/` entry, …)
/// and owns cleanup — wrap it in a [`TempEntry`] and [`disarm`](TempEntry::disarm)
/// only after this returns `Ok`.
///
/// # Panics
///
/// Panics if `dest` has no parent directory (via [`publish_temp`]).
pub async fn atomic_write(temp: &Path, dest: &Path, bytes: &[u8]) -> Result<(), AppError> {
    if let Some(parent) = temp.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    {
        let mut file = tokio::fs::File::create(temp).await?;
        file.write_all(bytes).await?;
        file.sync_all().await?;
    }
    publish_temp(temp, dest).await
}

/// Atomically write `bytes` to `dest` via a sibling temp.
///
/// Stages at `<dest>.tmp-<nonce>` (same directory ⇒ same filesystem), guarded by
/// a [`TempEntry`] so any early return unlinks the half-written file.
pub async fn atomic_write_sibling(dest: &Path, bytes: &[u8]) -> Result<(), AppError> {
    let parent = dest.parent().expect("dest has a parent directory");
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time before UNIX epoch")
        .as_nanos();
    let file_name = dest
        .file_name()
        .expect("dest has a file name")
        .to_string_lossy();
    let mut temp = TempEntry::new(parent.join(format!("{file_name}.tmp-{nonce:x}")));

    atomic_write(temp.path(), dest, bytes).await?;
    temp.disarm();
    Ok(())
}

/// Serialize `value` as JSON and [`atomic_write`] it via the caller's `temp` path.
///
/// The convenience index rows (and later bucket `metadata.json`) want — one call
/// from "in-memory struct" to "durably on disk", with the staging location left
/// to the caller.
pub async fn atomic_write_json<T: Serialize>(
    temp: &Path,
    dest: &Path,
    value: &T,
) -> Result<(), AppError> {
    let bytes = serde_json::to_vec(value)?;
    atomic_write(temp, dest, &bytes).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;
    use tempfile::TempDir;

    fn fresh_root() -> TempDir {
        TempDir::new().expect("temp root")
    }

    #[tokio::test]
    async fn publish_temp_moves_bytes_to_dest_and_consumes_the_temp() {
        let root = fresh_root();
        let temp = root.path().join("tmp").join("staged");
        let dest = root.path().join("objects").join("ab").join("final");

        tokio::fs::create_dir_all(temp.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&temp, b"durable-bytes").await.unwrap();

        publish_temp(&temp, &dest).await.expect("publish");

        assert_eq!(tokio::fs::read(&dest).await.unwrap(), b"durable-bytes");
        assert!(!tokio::fs::try_exists(&temp).await.unwrap());
    }

    #[tokio::test]
    async fn publish_temp_creates_missing_destination_parents() {
        let root = fresh_root();
        let temp = root.path().join("staged");
        let dest = root.path().join("a").join("b").join("c").join("blob");

        tokio::fs::write(&temp, b"nested").await.unwrap();
        publish_temp(&temp, &dest).await.expect("publish");

        assert!(dest.is_file());
        assert_eq!(tokio::fs::read(&dest).await.unwrap(), b"nested");
    }

    #[tokio::test]
    async fn atomic_write_stages_at_the_caller_chosen_temp_path() {
        let root = fresh_root();
        let temp = root.path().join("tmp").join("row.json");
        let dest = root.path().join("objects").join("row.json");
        let mut guard = TempEntry::new(temp.clone());

        atomic_write(guard.path(), &dest, b"{\"k\":1}")
            .await
            .expect("write");
        guard.disarm();

        assert_eq!(tokio::fs::read(&dest).await.unwrap(), b"{\"k\":1}");
        assert!(!tokio::fs::try_exists(&temp).await.unwrap());
    }

    #[tokio::test]
    async fn atomic_write_failure_leaves_temp_for_the_caller_guard_to_reap() {
        let root = fresh_root();
        // Make dest's parent a file so create_dir_all(dest.parent()) fails.
        let blocker = root.path().join("not-a-dir");
        tokio::fs::write(&blocker, b"file").await.unwrap();

        let temp = root.path().join("tmp").join("orphan");
        let dest = blocker.join("dest");
        let guard = TempEntry::new(temp.clone());

        let err = atomic_write(guard.path(), &dest, b"lost").await;
        assert!(err.is_err(), "publish must fail when dest parent is a file");
        assert!(
            tokio::fs::try_exists(&temp).await.unwrap(),
            "temp must still exist so TempEntry can clean it up"
        );
        drop(guard);
        assert!(
            !tokio::fs::try_exists(&temp).await.unwrap(),
            "TempEntry Drop must unlink the half-written temp"
        );
    }

    #[tokio::test]
    async fn atomic_write_sibling_publishes_without_leaving_tmp_files() {
        let root = fresh_root();
        let dest = root.path().join("meta").join("bucket.json");

        atomic_write_sibling(&dest, b"sibling-v1")
            .await
            .expect("sibling write");

        assert_eq!(tokio::fs::read(&dest).await.unwrap(), b"sibling-v1");

        let leftovers: Vec<_> = std::fs::read_dir(dest.parent().unwrap())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .filter(|n| n.to_string_lossy().contains(".tmp-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "no sibling temps should remain after a successful publish: {leftovers:?}"
        );
    }

    #[tokio::test]
    async fn atomic_write_sibling_overwrites_dest_with_the_latest_bytes() {
        let root = fresh_root();
        let dest = root.path().join("pointer.json");

        atomic_write_sibling(&dest, b"v1").await.expect("v1");
        atomic_write_sibling(&dest, b"v2").await.expect("v2");

        assert_eq!(tokio::fs::read(&dest).await.unwrap(), b"v2");
    }

    #[tokio::test]
    async fn atomic_write_json_round_trips_a_serializable_value() {
        #[derive(Debug, PartialEq, Serialize, Deserialize)]
        struct Row {
            key: String,
            n: u32,
        }

        let root = fresh_root();
        let temp = root.path().join("tmp").join("row.json");
        let dest = root.path().join("objects").join("row.json");
        let mut guard = TempEntry::new(temp);
        let value = Row {
            key: "a/b".into(),
            n: 7,
        };

        atomic_write_json(guard.path(), &dest, &value)
            .await
            .expect("json write");
        guard.disarm();

        let got: Row = serde_json::from_slice(&tokio::fs::read(&dest).await.unwrap()).unwrap();
        assert_eq!(got, value);
    }
}
