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

use std::str::FromStr;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::error::AppError;

pub use crate::naming::{Bucket, Key};

/// Opaque id of one version under a key. Monotonic per key; never reused.
pub type VersionId = u64;

/// What the index digest names on disk — whole object bytes, or a CDC manifest.
///
/// Existing index JSON without this field deserializes as [`Whole`](Self::Whole)
/// via `#[serde(default)]` on [`VersionKind::Live`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BlobKind {
    /// `digest` is SHA-256 of the full plaintext object (V1 today).
    #[default]
    Whole,
    /// `digest` is SHA-256 of a [`crate::manifest::Manifest`] blob; logical
    /// bytes are the concatenation of the manifest's chunk digests.
    Manifest,
}

/// A content digest: the SHA-256 of an object's bytes, hex-encoded. In a
/// content-addressed store this *is* the blob's name on disk (V1), which is what
/// makes dedup free: identical bytes produce the same digest, stored once.
///
/// With CDC, a live version may store a *manifest* digest here instead
/// ([`BlobKind::Manifest`]) — still a CAS name, finer grain underneath.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Digest(pub String);

impl Digest {
    /// Raw SHA-256 size in bytes (before hex encoding).
    pub const BYTE_LEN: usize = 32;

    /// Hex-encoded length of a digest string (`2 * BYTE_LEN` → 64 chars).
    pub const LEN: usize = Self::BYTE_LEN * 2;

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Parse a hex digest string into a [`Digest`].
    ///
    /// Accepts upper- or lower-case hex; stores lowercase so on-disk needle
    /// headers and path shards stay consistent with `hex::encode`.
    ///
    /// A well-formed digest is exactly [`Self::LEN`] ASCII hex digits
    /// (`0-9`, `a-f`, `A-F`) — shape only, not a check that bytes hash here.
    ///
    /// # Errors
    ///
    /// Returns [`AppError::InvalidRequest`] if the string is malformed.
    pub fn parse(s: impl AsRef<str>) -> Result<Self, AppError> {
        let s = s.as_ref();
        if s.len() != Self::LEN || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(AppError::InvalidRequest(format!(
                "digest must be exactly {} ASCII hex characters, got len={} {:?}",
                Self::LEN,
                s.len(),
                s.chars().take(16).collect::<String>()
            )));
        }
        Ok(Self(s.to_ascii_lowercase()))
    }
}

impl FromStr for Digest {
    type Err = AppError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Self::parse(s)
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
        /// Defaults to [`BlobKind::Whole`] for index rows written before CDC.
        #[serde(default)]
        blob_kind: BlobKind,
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
        Self::live_with_kind(id, digest, etag, size, content_type, BlobKind::Whole)
    }

    pub fn live_with_kind(
        id: VersionId,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
        blob_kind: BlobKind,
    ) -> Self {
        Self {
            id,
            last_modified: Utc::now(),
            kind: VersionKind::Live {
                digest,
                etag,
                size,
                content_type,
                blob_kind,
            },
        }
    }

    pub fn as_live(&self) -> Option<(&Digest, &ETag, u64, &str, BlobKind)> {
        match &self.kind {
            VersionKind::Live {
                digest,
                etag,
                size,
                content_type,
                blob_kind,
            } => Some((digest, etag, *size, content_type.as_str(), *blob_kind)),
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
    pub blob_kind: BlobKind,
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
        Self::new_live_with_kind(
            bucket,
            key,
            digest,
            etag,
            size,
            content_type,
            BlobKind::Whole,
        )
    }

    pub fn new_live_with_kind(
        bucket: Bucket,
        key: Key,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
        blob_kind: BlobKind,
    ) -> Self {
        let entry = VersionEntry::live_with_kind(1, digest, etag, size, content_type, blob_kind);
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
        let (digest, etag, size, content_type, blob_kind) =
            entry.as_live().ok_or(AppError::NoSuchKey)?;
        Ok(ResolvedObject {
            bucket: self.bucket.clone(),
            key: self.key.clone(),
            version_id: entry.id,
            digest: digest.clone(),
            etag: etag.clone(),
            size,
            content_type: content_type.to_string(),
            last_modified: entry.last_modified,
            blob_kind,
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
        self.append_live_with_kind(digest, etag, size, content_type, BlobKind::Whole)
    }

    pub fn append_live_with_kind(
        &mut self,
        digest: Digest,
        etag: ETag,
        size: u64,
        content_type: String,
        blob_kind: BlobKind,
    ) -> VersionId {
        let id = {
            let highest = self.versions.iter().map(|v| v.id).max().unwrap_or(0);
            let id = self.next_id.max(highest + 1);
            self.next_id = id + 1;
            id
        };
        let entry = VersionEntry::live_with_kind(id, digest, etag, size, content_type, blob_kind);
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
    use proptest::prelude::*;
    use std::collections::HashSet;

    fn live_meta() -> ObjectMeta {
        ObjectMeta::new_live(
            Bucket::from_trusted("photos"),
            Key::from_trusted("a.jpg"),
            Digest("aaa".into()),
            ETag("etag-a".into()),
            10,
            "image/jpeg".into(),
        )
    }

    #[test]
    fn digest_parse_requires_exact_hex_len() {
        assert!(Digest::parse("ab".repeat(32)).is_ok());
        assert!(
            Digest::parse("AB".repeat(32)).is_ok(),
            "uppercase hex is ok"
        );
        assert!(Digest::parse("aaa").is_err());
        assert!(Digest::parse("ag".repeat(32)).is_err(), "g is not hex");
        assert!(Digest::parse(format!("{}x", "a".repeat(63))).is_err());
    }

    #[test]
    fn digest_parse_normalizes_to_lowercase() {
        let upper = "AB".repeat(32);
        let d = Digest::parse(upper).unwrap();
        assert_eq!(d.as_str(), "ab".repeat(32));
    }

    #[test]
    fn digest_parse_rejects_malformed() {
        assert!(matches!(
            Digest::parse("not-a-digest"),
            Err(AppError::InvalidRequest(_))
        ));
        assert!(Digest::parse("short").is_err());
    }

    /// A live payload for `append_live` / `new_live`, keyed by a small seed so
    /// digests stay distinct across a sequence of overwrites.
    fn sample_live(seed: u64) -> (Digest, ETag, u64, String) {
        (
            Digest(format!("digest-{seed}")),
            ETag(format!("etag-{seed}")),
            seed,
            format!("type/{seed}"),
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

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(64))]

        /// `from_query(None)` is always Latest; any concrete id pins that version.
        #[test]
        fn prop_object_ref_from_query(version_id in prop::option::of(any::<VersionId>())) {
            match version_id {
                None => prop_assert_eq!(ObjectRef::from_query(None), ObjectRef::Latest),
                Some(id) => {
                    prop_assert_eq!(ObjectRef::from_query(Some(id)), ObjectRef::Version(id))
                }
            }
        }

        /// `new_live` seeds a one-version history: id 1 is latest, `next_id` is 2,
        /// and resolve_live returns the exact payload.
        #[test]
        fn prop_new_live_seeds_version_one(
            bucket in "[a-z0-9-]{3,20}",
            key in "[a-zA-Z0-9/._-]{1,30}",
            seed in 0u64..10_000,
        ) {
            let (digest, etag, size, content_type) = sample_live(seed);
            let meta = ObjectMeta::new_live(
                Bucket::from_trusted(bucket.clone()),
                Key::from_trusted(key.clone()),
                digest.clone(),
                etag.clone(),
                size,
                content_type.clone(),
            );

            prop_assert_eq!(meta.bucket.as_str(), bucket.as_str());
            prop_assert_eq!(meta.key.as_str(), key.as_str());
            prop_assert_eq!(meta.latest, 1);
            prop_assert_eq!(meta.next_id, 2);
            prop_assert_eq!(meta.versions.len(), 1);

            let got = meta.resolve_live(ObjectRef::Latest).expect("latest must be live");
            prop_assert_eq!(got.version_id, 1);
            prop_assert_eq!(got.digest, digest);
            prop_assert_eq!(got.etag, etag);
            prop_assert_eq!(got.size, size);
            prop_assert_eq!(got.content_type, content_type);
        }

        /// Every append flips `latest` to the new id, keeps prior history, and
        /// never reuses an id — even when some versions were removed mid-sequence.
        #[test]
        fn prop_append_ids_are_unique_and_never_reused(
            appends in 1usize..12,
            // Which append indices (0-based among the appends after new_live) to
            // delete before the next append — exercises the next_id monotonicity law.
            delete_mask in any::<u16>(),
        ) {
            let mut meta = ObjectMeta::new_live(
                Bucket::from_trusted("photos"),
                Key::from_trusted("k"),
                Digest("d0".into()),
                ETag("e0".into()),
                0,
                "application/octet-stream".into(),
            );
            let mut seen: HashSet<VersionId> = HashSet::from([1]);
            let mut issued: Vec<VersionId> = vec![1];

            for i in 0..appends {
                // Optionally drop an earlier version before appending — the freed
                // id must never come back.
                if delete_mask & (1u16 << (i % 16)) != 0 {
                    if let Some(&victim) = issued.first() {
                        if meta.versions.len() > 1 {
                            meta.remove_version(victim);
                            issued.retain(|&id| id != victim);
                        }
                    }
                }

                let (digest, etag, size, ct) = sample_live((i as u64) + 1);
                let id = meta.append_live(digest.clone(), etag, size, ct);
                prop_assert!(
                    seen.insert(id),
                    "version id {id} was reused after being issued"
                );
                issued.push(id);
                prop_assert_eq!(meta.latest, id, "append must flip latest to the new id");
                prop_assert!(
                    meta.next_id > id,
                    "next_id must climb past every issued id"
                );

                let got = meta
                    .resolve_live(ObjectRef::Version(id))
                    .expect("just-appended version must resolve live");
                prop_assert_eq!(got.digest, digest);
                prop_assert_eq!(got.version_id, id);
            }

            // Every surviving version id is unique and present exactly once.
            let ids: Vec<_> = meta.versions.iter().map(|v| v.id).collect();
            let unique: HashSet<_> = ids.iter().copied().collect();
            prop_assert_eq!(ids.len(), unique.len(), "history must never contain duplicate ids");
        }

        /// `resolve(Version(id))` finds that entry when present; missing ids and
        /// delete markers both make `resolve_live` return NoSuchKey.
        #[test]
        fn prop_resolve_live_rejects_missing_and_delete_markers(
            seed in 0u64..1_000,
            ghost in 100u64..1_000,
        ) {
            let (digest, etag, size, ct) = sample_live(seed);
            let mut meta = ObjectMeta::new_live(
                Bucket::from_trusted("b"),
                Key::from_trusted("k"),
                digest,
                etag,
                size,
                ct,
            );

            // Pin a historical live version, then overwrite so Latest ≠ Version(1).
            meta.append_live(
                Digest("d2".into()),
                ETag("e2".into()),
                2,
                "text/plain".into(),
            );
            prop_assert!(meta.resolve_live(ObjectRef::Version(1)).is_ok());
            prop_assert!(
                matches!(
                    meta.resolve_live(ObjectRef::Version(ghost)),
                    Err(AppError::NoSuchKey)
                ),
                "a never-issued id must be NoSuchKey"
            );

            // Replace the latest live entry with a delete marker in-place.
            let latest_id = meta.latest;
            if let Some(entry) = meta.versions.iter_mut().find(|v| v.id == latest_id) {
                entry.kind = VersionKind::DeleteMarker;
            }
            prop_assert!(
                matches!(
                    meta.resolve_live(ObjectRef::Latest),
                    Err(AppError::NoSuchKey)
                ),
                "a delete marker must not resolve as a live object"
            );
            prop_assert!(
                meta.resolve(ObjectRef::Latest).is_some(),
                "resolve still finds the delete-marker entry by id"
            );
        }

        /// Removing the current latest retargets to the newest surviving id;
        /// removing a non-latest id leaves `latest` alone.
        #[test]
        fn prop_remove_version_retargets_latest(
            extra in 1usize..8,
            remove_latest in any::<bool>(),
        ) {
            let mut meta = ObjectMeta::new_live(
                Bucket::from_trusted("b"),
                Key::from_trusted("k"),
                Digest("d0".into()),
                ETag("e0".into()),
                0,
                "application/octet-stream".into(),
            );
            for i in 0..extra {
                let (d, e, s, ct) = sample_live((i as u64) + 1);
                meta.append_live(d, e, s, ct);
            }

            let victim = if remove_latest {
                meta.latest
            } else {
                // Prefer an older survivor when history has more than one entry.
                meta.versions
                    .iter()
                    .map(|v| v.id)
                    .find(|&id| id != meta.latest)
                    .unwrap_or(meta.latest)
            };
            let previous_latest = meta.latest;
            prop_assert!(meta.remove_version(victim));

            if victim == previous_latest {
                let expected = meta.versions.iter().map(|v| v.id).max().unwrap_or(0);
                prop_assert_eq!(
                    meta.latest, expected,
                    "removing latest must retarget to the newest survivor"
                );
            } else {
                prop_assert_eq!(
                    meta.latest, previous_latest,
                    "removing a non-latest version must leave latest unchanged"
                );
            }
            prop_assert!(
                meta.versions.iter().all(|v| v.id != victim),
                "removed id must be gone from history"
            );
            // Removing again is a no-op.
            prop_assert!(!meta.remove_version(victim));
        }

        /// `digests()` yields exactly the digests of Live versions — never a
        /// delete marker — and matches a manual filter over `versions`.
        #[test]
        fn prop_digests_skips_delete_markers(live_count in 1usize..8, marker_at in any::<prop::sample::Index>()) {
            let mut meta = ObjectMeta::new_live(
                Bucket::from_trusted("b"),
                Key::from_trusted("k"),
                Digest("d0".into()),
                ETag("e0".into()),
                0,
                "application/octet-stream".into(),
            );
            for i in 1..live_count {
                let (d, e, s, ct) = sample_live(i as u64);
                meta.append_live(d, e, s, ct);
            }
            // Flip one entry into a delete marker.
            let at = marker_at.index(meta.versions.len());
            meta.versions[at].kind = VersionKind::DeleteMarker;

            let expected: Vec<&Digest> = meta
                .versions
                .iter()
                .filter_map(|v| match &v.kind {
                    VersionKind::Live { digest, .. } => Some(digest),
                    VersionKind::DeleteMarker => None,
                })
                .collect();
            let got: Vec<&Digest> = meta.digests().collect();
            prop_assert_eq!(got, expected);
        }

        /// ObjectMeta survives a JSON round-trip with the same identity fields,
        /// version ids, and digests (timestamps may keep chrono's precision).
        #[test]
        fn prop_object_meta_json_round_trip(seed in 0u64..1_000, overwrites in 0usize..6) {
            let (digest, etag, size, ct) = sample_live(seed);
            let mut meta = ObjectMeta::new_live(
                Bucket::from_trusted("photos"),
                Key::from_trusted(format!("obj-{seed}")),
                digest,
                etag,
                size,
                ct,
            );
            for i in 0..overwrites {
                let (d, e, s, c) = sample_live(seed + 1 + i as u64);
                meta.append_live(d, e, s, c);
            }

            let json = serde_json::to_string(&meta).expect("serialize");
            let back: ObjectMeta = serde_json::from_str(&json).expect("deserialize");

            prop_assert_eq!(&back.bucket, &meta.bucket);
            prop_assert_eq!(&back.key, &meta.key);
            prop_assert_eq!(back.latest, meta.latest);
            prop_assert_eq!(back.next_id, meta.next_id);
            prop_assert_eq!(back.versions.len(), meta.versions.len());
            for (a, b) in meta.versions.iter().zip(back.versions.iter()) {
                prop_assert_eq!(a.id, b.id);
                prop_assert_eq!(&a.kind, &b.kind);
            }
        }
    }
}
