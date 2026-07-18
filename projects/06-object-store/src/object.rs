//! Shared object-store types: identities, digests, and metadata.
//!
//! These are the values the verticals pass around — V2 streams a body and hands
//! back a [`Digest`] + [`ETag`] + size; V3 records that as an [`ObjectMeta`] row
//! pointing a `(bucket, key)` at the blob; V4 assembles parts into the same.
//!
//! ## Latest vs versions
//!
//! A key owns a version history ([`VersionEntry`]) and a mutable [`ObjectMeta::latest`]
//! pointer into it. API calls name which version they mean with [`ObjectRef`]:
//! `Latest` for the hot path, `Version(id)` to pin a historical one. Each entry is
//! either a live object ([`VersionKind::Live`]) or a delete marker.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::error::AppError;

/// A bucket name — the top-level container (like an S3 bucket).
pub type Bucket = String;

/// An object key: the full path within a bucket. It may contain `/`, but the
/// keyspace is **flat** — the slashes are only meaningful to the prefix/delimiter
/// listing in V3, never a real directory tree on disk.
pub type Key = String;

/// Opaque id of one version under a key. Monotonic per key; never reused.
pub type VersionId = u64;

/// A content digest: the SHA-256 of an object's bytes, hex-encoded. In a
/// content-addressed store this *is* the blob's name on disk (V1), which is what
/// makes dedup free: identical bytes produce the same digest, stored once.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Digest(pub String);

impl Digest {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The S3 `ETag` of an object. **Not** the same thing as the content digest:
///   - single PUT → `hex(md5(bytes))` (V2).
///   - multipart  → `hex(md5(concat(decoded part md5s)))` + `"-" + N`, where N
///     is the part count (V4). The `-N` suffix is how a client knows the object
///     was multipart and must not re-MD5 it to verify.
///
/// Clients use it for conditional requests (`If-None-Match`) and integrity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ETag(pub String);

impl ETag {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Which version of a key an API call is addressing.
///
/// This is the request/resolve discriminator — not an on-disk type. Persist the
/// history as [`ObjectMeta`]; decide *which* entry with this enum.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObjectRef {
    /// The current version ([`ObjectMeta::latest`]).
    Latest,
    /// A specific historical version id.
    Version(VersionId),
}

impl ObjectRef {
    /// Build from an optional `versionId` query param (`None` → [`Latest`](Self::Latest)).
    pub fn from_query(version_id: Option<VersionId>) -> Self {
        match version_id {
            Some(id) => Self::Version(id),
            None => Self::Latest,
        }
    }
}

/// What one version in the history *is*.
///
/// A delete marker is not "a live object with a flag" — it has no digest to open.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum VersionKind {
    Live {
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
    },
    DeleteMarker,
}

/// One immutable version under a key.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VersionEntry {
    pub id: VersionId,
    pub last_modified: DateTime<Utc>,
    pub kind: VersionKind,
}

impl VersionEntry {
    pub fn live(
        id: VersionId,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
    ) -> Self {
        Self {
            id,
            last_modified: Utc::now(),
            kind: VersionKind::Live {
                digest,
                etag,
                size,
                content_type,
            },
        }
    }

    pub fn as_live(&self) -> Option<(&Digest, &ETag, u64, &str)> {
        match &self.kind {
            VersionKind::Live {
                digest,
                etag,
                size,
                content_type,
            } => Some((digest, etag, *size, content_type.as_str())),
            VersionKind::DeleteMarker => None,
        }
    }
}

/// Hot-path view of a **live** version — what GET/HEAD/list need after resolve.
#[derive(Debug, Clone)]
pub struct ResolvedObject {
    pub bucket: Bucket,
    pub key: Key,
    pub version_id: VersionId,
    pub digest: Digest,
    pub etag: ETag,
    pub size: u64,
    pub content_type: String,
    pub last_modified: DateTime<Utc>,
}

/// What we persist about a stored object — the index row (V3) for one key.
///
/// `latest` is the mutable pointer; `versions` is append-only history. An
/// overwrite appends a new [`VersionKind::Live`] and flips `latest`.
///
/// `next_id` is a per-key monotonic counter that only ever climbs. It is the
/// source of truth for the next [`VersionId`] — deliberately *not* recomputed
/// from `versions`, so deleting the newest version can't rewind it and hand the
/// same id to a different object. This is what makes [`VersionId`]'s "never
/// reused" contract actually hold.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectMeta {
    pub bucket: Bucket,
    pub key: Key,
    pub latest: VersionId,
    pub versions: Vec<VersionEntry>,
    #[serde(default)]
    pub next_id: VersionId,
}

impl ObjectMeta {
    /// Fresh key with a single live version (id `1`).
    pub fn new_live(
        bucket: Bucket,
        key: Key,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
    ) -> Self {
        let entry = VersionEntry::live(1, digest, etag, size, content_type);
        Self {
            bucket,
            key,
            latest: entry.id,
            versions: vec![entry],
            next_id: 2,
        }
    }

    /// Look up the version named by `object_ref`, or `None` if that id is missing.
    pub fn resolve(&self, object_ref: ObjectRef) -> Option<&VersionEntry> {
        let id = match object_ref {
            ObjectRef::Latest => self.latest,
            ObjectRef::Version(id) => id,
        };
        self.versions.iter().find(|v| v.id == id)
    }

    /// Resolve to a live object, or [`AppError::NoSuchKey`] if the ref is missing
    /// or points at a delete marker.
    pub fn resolve_live(&self, object_ref: ObjectRef) -> Result<ResolvedObject, AppError> {
        let entry = self.resolve(object_ref).ok_or(AppError::NoSuchKey)?;
        let (digest, etag, size, content_type) = entry.as_live().ok_or(AppError::NoSuchKey)?;
        Ok(ResolvedObject {
            bucket: self.bucket.clone(),
            key: self.key.clone(),
            version_id: entry.id,
            digest: digest.clone(),
            etag: etag.clone(),
            size,
            content_type: content_type.to_string(),
            last_modified: entry.last_modified,
        })
    }

    /// The current live object, if `latest` names a [`VersionKind::Live`].
    pub fn latest_live(&self) -> Option<ResolvedObject> {
        self.resolve_live(ObjectRef::Latest).ok()
    }

    pub fn append_live(
        &mut self,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
    ) -> VersionId {
        let id = {
            let highest = self.versions.iter().map(|v| v.id).max().unwrap_or(0);
            let id = self.next_id.max(highest + 1);
            self.next_id = id + 1;
            id
        };
        let entry = VersionEntry::live(id, digest, etag, size, content_type);
        self.versions.push(entry);
        self.latest = id;
        id
    }

    /// Remove one version by id. If it was `latest`, retarget to the newest
    /// remaining entry (or leave `latest` unchanged if the history is empty —
    /// caller should drop the index row).
    pub fn remove_version(&mut self, id: VersionId) -> bool {
        let before = self.versions.len();
        self.versions.retain(|v| v.id != id);
        if self.versions.len() == before {
            return false;
        }
        if self.latest == id {
            self.latest = self.versions.iter().map(|v| v.id).max().unwrap_or(0);
        }
        true
    }

    pub fn digests(&self) -> impl Iterator<Item = &Digest> {
        self.versions.iter().filter_map(|v| match &v.kind {
            VersionKind::Live { digest, .. } => Some(digest),
            VersionKind::DeleteMarker => None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn live_meta() -> ObjectMeta {
        ObjectMeta::new_live(
            "photos".into(),
            "a.jpg".into(),
            Digest("aaa".into()),
            ETag("etag-a".into()),
            10,
            "image/jpeg".into(),
        )
    }

    #[test]
    fn resolve_latest_returns_the_current_live_version() {
        let meta = live_meta();
        let got = meta.resolve_live(ObjectRef::Latest).unwrap();
        assert_eq!(got.version_id, 1);
        assert_eq!(got.digest.as_str(), "aaa");
        assert_eq!(got.size, 10);
    }

    #[test]
    fn append_live_flips_latest_and_keeps_history() {
        let mut meta = live_meta();
        meta.append_live(
            Digest("bbb".into()),
            ETag("etag-b".into()),
            20,
            "image/jpeg".into(),
        );

        assert_eq!(meta.versions.len(), 2);
        assert_eq!(meta.latest, 2);
        assert_eq!(
            meta.resolve_live(ObjectRef::Latest)
                .unwrap()
                .digest
                .as_str(),
            "bbb"
        );
        assert_eq!(
            meta.resolve_live(ObjectRef::Version(1))
                .unwrap()
                .digest
                .as_str(),
            "aaa"
        );
    }

    /// Regression for the `max(ids) + 1` counter: deleting the newest version
    /// then appending must NOT reuse the just-freed id. With a recomputed
    /// counter this appended `2` again (aliasing a since-deleted object); the
    /// monotonic `next_id` gives `3` and honors `VersionId`'s "never reused".
    #[test]
    fn appended_id_is_never_reused_after_deleting_the_tail() {
        let mut meta = live_meta(); // v1, next_id = 2
        meta.append_live(
            Digest("bbb".into()),
            ETag("etag-b".into()),
            20,
            "image/jpeg".into(),
        );
        assert_eq!(meta.latest, 2, "the overwrite took id 2");

        // Delete the newest version — the counter must not rewind to it.
        assert!(meta.remove_version(2));
        assert_eq!(meta.latest, 1, "latest retargets to the newest survivor");

        let reappended = meta.append_live(
            Digest("ccc".into()),
            ETag("etag-c".into()),
            30,
            "image/jpeg".into(),
        );
        assert_eq!(
            reappended, 3,
            "the freed id 2 must never be handed out again"
        );
    }

    /// An entry persisted before `next_id` existed deserializes with `next_id ==
    /// 0`; the self-heal must still hand out an id past the highest present one,
    /// never colliding with an existing version.
    #[test]
    fn legacy_entry_without_next_id_self_heals() {
        // Build a two-version entry, then strip `next_id` to mimic JSON written
        // before the field existed — more robust than hand-typing the tagged enum.
        let mut current = live_meta();
        current.append_live(
            Digest("bbb".into()),
            ETag("etag-b".into()),
            20,
            "image/jpeg".into(),
        );
        let mut value = serde_json::to_value(&current).unwrap();
        value.as_object_mut().unwrap().remove("next_id");

        let mut meta: ObjectMeta = serde_json::from_value(value).expect("legacy JSON deserializes");
        assert_eq!(meta.next_id, 0, "missing field defaults to 0");

        let id = meta.append_live(
            Digest("ccc".into()),
            ETag("etag-c".into()),
            30,
            "image/jpeg".into(),
        );
        assert_eq!(
            id, 3,
            "self-heal skips past the highest existing id, no collision"
        );
    }

    #[test]
    fn object_ref_from_query() {
        assert_eq!(ObjectRef::from_query(None), ObjectRef::Latest);
        assert_eq!(ObjectRef::from_query(Some(7)), ObjectRef::Version(7));
    }
}
