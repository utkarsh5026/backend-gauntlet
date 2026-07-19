//! Bucket-level metadata — the first durable state a bucket owns beyond "the
//! directory exists".
//!
//! Today a bucket is just `index/<bucket>/` (see [`Index::create_bucket`]). This
//! adds one document at the bucket root, `index/<bucket>/metadata.json`, holding
//! the things S3 keeps per bucket. Placement is deliberate: it's a **sibling** of
//! the `objects/` dir where key rows live, so it can never collide with a
//! user-PUT key (every key lands under `objects/`).
//!
//! The **absent = default** rule keeps this backward-compatible: buckets created
//! before this feature have no file, so [`BucketMetadata::load`] returns a fresh
//! default instead of erroring. Only fields with a live reader live here
//! ([`schema_version`], [`created_at`], [`lifecycle`]) — everything else S3 has
//! (ACL, CORS, encryption, tags) waits until a consumer exists.
//!
//! [`Index::create_bucket`]: crate::index::Index::create_bucket
//! [`schema_version`]: BucketMetadata::schema_version
//! [`created_at`]: BucketMetadata::created_at
//! [`lifecycle`]: BucketMetadata::lifecycle

use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::durable::atomic_write_sibling;
use crate::error::AppError;
use crate::lifecycle::LifecyclePolicy;

/// The current on-disk schema. Bump when a change isn't purely additive, and
/// branch on it in [`BucketMetadata::load`] to migrate old files forward.
pub const CURRENT_SCHEMA_VERSION: u32 = 1;

/// What we persist about a bucket, at `index/<bucket>/metadata.json`.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BucketMetadata {
    /// On-disk format version — makes the next field an additive migration, not
    /// a break. Defaults to `1` for files written before this field existed.
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,

    /// When the bucket was created — S3 surfaces this as `CreationDate` in
    /// `ListBuckets`. There's no other durable record of a bucket's birth.
    pub created_at: DateTime<Utc>,

    /// The owner's lifecycle rules. Empty by default (nothing ages).
    #[serde(default)]
    pub lifecycle: LifecyclePolicy,
}

const fn default_schema_version() -> u32 {
    CURRENT_SCHEMA_VERSION
}

impl BucketMetadata {
    /// Base name of the metadata document within a bucket directory.
    const FILE_NAME: &str = "metadata.json";

    /// A brand-new bucket's metadata: born `now`, no lifecycle rules yet.
    /// Written by `create_bucket` so every bucket always has a file on disk.
    pub fn new() -> Self {
        Self {
            schema_version: CURRENT_SCHEMA_VERSION,
            created_at: chrono::Utc::now(),
            lifecycle: LifecyclePolicy::default(),
        }
    }

    /// Absolute path of the metadata doc given a bucket's directory
    /// (`index/<bucket>`).
    pub fn path_in(bucket_dir: &Path) -> PathBuf {
        bucket_dir.join(Self::FILE_NAME)
    }

    /// Load a bucket's metadata, or a fresh [`BucketMetadata::new`] default if
    /// the file is **absent** (a pre-feature bucket).
    ///
    /// Absent is not an error — that's the backward-compat contract. A file that
    /// exists but won't parse *is* an error (don't silently discard a corrupt
    /// policy). If `schema_version` is older than [`CURRENT_SCHEMA_VERSION`],
    /// migrate here before returning.
    pub async fn load(bucket_dir: &Path) -> Result<Self, AppError> {
        let path = Self::path_in(bucket_dir);
        let bytes = match tokio::fs::read(&path).await {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::new());
            }
            Err(err) => return Err(err.into()),
        };
        let mut meta = serde_json::from_slice::<Self>(&bytes)?;
        if meta.schema_version < CURRENT_SCHEMA_VERSION {
            meta.schema_version = CURRENT_SCHEMA_VERSION;
            atomic_write_sibling(&path, &serde_json::to_vec(&meta)?).await?;
        }
        Ok(meta)
    }

    /// Durably persist this metadata via a sibling temp next to
    /// `metadata.json` ([`durable::atomic_write_sibling`]).
    ///
    /// Unlike index rows, bucket metadata is not scanned by GC, so a sibling
    /// under the bucket dir is enough — same filesystem, no duplicated nonce /
    /// [`TempEntry`](crate::durable::TempEntry) logic.
    ///
    /// [`durable::atomic_write_sibling`]: crate::durable::atomic_write_sibling
    pub async fn store(&self, bucket_dir: &Path) -> Result<(), AppError> {
        atomic_write_sibling(&Self::path_in(bucket_dir), &serde_json::to_vec(self)?).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lifecycle::LifecycleRule;
    use tempfile::TempDir;

    fn bucket_dir() -> TempDir {
        TempDir::new().expect("temp bucket dir")
    }

    fn sample_rule() -> LifecycleRule {
        LifecycleRule {
            id: "cool-then-delete".into(),
            enabled: true,
            prefix: Some("logs/".into()),
            tier_after_days: Some(30),
            expire_after_days: Some(365),
            noncurrent_expire_after_days: None,
            abort_multipart_after_days: None,
        }
    }

    #[test]
    fn new_sets_current_schema_and_empty_lifecycle() {
        let meta = BucketMetadata::new();
        assert_eq!(meta.schema_version, CURRENT_SCHEMA_VERSION);
        assert!(meta.lifecycle.rules.is_empty());
        assert!(meta.created_at <= Utc::now());
    }

    #[test]
    fn path_in_joins_metadata_json() {
        let dir = Path::new("/data/index/my-bucket");
        assert_eq!(
            BucketMetadata::path_in(dir),
            PathBuf::from("/data/index/my-bucket/metadata.json")
        );
    }

    #[test]
    fn deserialize_defaults_omitted_schema_and_lifecycle() {
        let created_at = Utc::now();
        let json = format!(r#"{{"created_at":"{}"}}"#, created_at.to_rfc3339());
        let meta: BucketMetadata = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(meta.schema_version, CURRENT_SCHEMA_VERSION);
        assert!(meta.lifecycle.rules.is_empty());
        assert_eq!(meta.created_at, created_at);
    }

    #[tokio::test]
    async fn store_then_load_round_trips_lifecycle_policy() {
        let root = bucket_dir();
        let mut meta = BucketMetadata::new();
        meta.lifecycle.rules.push(sample_rule());

        meta.store(root.path()).await.expect("store");
        let loaded = BucketMetadata::load(root.path()).await.expect("load");

        assert_eq!(loaded.schema_version, meta.schema_version);
        assert_eq!(loaded.created_at, meta.created_at);
        assert_eq!(loaded.lifecycle.rules.len(), 1);
        assert_eq!(loaded.lifecycle.rules[0].id, "cool-then-delete");
        assert_eq!(
            loaded.lifecycle.rules[0].prefix.as_deref(),
            Some("logs/")
        );
        assert_eq!(loaded.lifecycle.rules[0].tier_after_days, Some(30));
        assert_eq!(loaded.lifecycle.rules[0].expire_after_days, Some(365));
    }

    #[tokio::test]
    async fn store_overwrites_previous_metadata() {
        let root = bucket_dir();
        let mut first = BucketMetadata::new();
        first.lifecycle.rules.push(sample_rule());
        first.store(root.path()).await.expect("store v1");

        let second = BucketMetadata::new();
        second.store(root.path()).await.expect("store v2");

        let loaded = BucketMetadata::load(root.path()).await.expect("load");
        assert!(loaded.lifecycle.rules.is_empty());
        assert_eq!(loaded.created_at, second.created_at);
    }

    #[tokio::test]
    async fn store_leaves_no_sibling_temp_files() {
        let root = bucket_dir();
        BucketMetadata::new()
            .store(root.path())
            .await
            .expect("store");

        let leftovers: Vec<_> = std::fs::read_dir(root.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .filter(|n| n.to_string_lossy().contains(".tmp-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "no sibling temps should remain after store: {leftovers:?}"
        );
        assert!(BucketMetadata::path_in(root.path()).is_file());
    }

    #[tokio::test]
    async fn load_returns_default_when_metadata_file_is_absent() {
        let root = bucket_dir();
        let loaded = BucketMetadata::load(root.path()).await.expect("load");
        assert_eq!(loaded.schema_version, CURRENT_SCHEMA_VERSION);
        assert!(loaded.lifecycle.rules.is_empty());
        assert!(!BucketMetadata::path_in(root.path()).exists());
    }

    #[tokio::test]
    async fn load_rejects_corrupt_json() {
        let root = bucket_dir();
        tokio::fs::write(BucketMetadata::path_in(root.path()), b"{not-json")
            .await
            .unwrap();

        let err = BucketMetadata::load(root.path()).await;
        assert!(err.is_err(), "corrupt metadata must not be silently discarded");
    }

    #[tokio::test]
    async fn load_migrates_older_schema_version_and_rewrites() {
        let root = bucket_dir();
        let created_at = Utc::now();
        let stale = serde_json::json!({
            "schema_version": 0,
            "created_at": created_at,
            "lifecycle": { "rules": [] }
        });
        tokio::fs::write(
            BucketMetadata::path_in(root.path()),
            serde_json::to_vec(&stale).unwrap(),
        )
        .await
        .unwrap();

        let loaded = BucketMetadata::load(root.path()).await.expect("load");
        assert_eq!(loaded.schema_version, CURRENT_SCHEMA_VERSION);
        assert_eq!(loaded.created_at, created_at);

        let on_disk: BucketMetadata = serde_json::from_slice(
            &tokio::fs::read(BucketMetadata::path_in(root.path()))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            on_disk.schema_version, CURRENT_SCHEMA_VERSION,
            "migration must rewrite the file, not only the in-memory value"
        );
    }
}
