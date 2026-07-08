//! Shared object-store types: identities, digests, and metadata.
//!
//! These are the values the verticals pass around — V2 streams a body and hands
//! back a [`Digest`] + [`ETag`] + size; V3 records that as an [`ObjectMeta`] row
//! pointing a `(bucket, key)` at the blob; V4 assembles parts into the same.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// A bucket name — the top-level container (like an S3 bucket).
pub type Bucket = String;

/// An object key: the full path within a bucket. It may contain `/`, but the
/// keyspace is **flat** — the slashes are only meaningful to the prefix/delimiter
/// listing in V3, never a real directory tree on disk.
pub type Key = String;

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

/// What we persist about a stored object — the index row (V3) that points a
/// `(bucket, key)` at the blob backing it, plus the metadata S3 returns on
/// HEAD/GET.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectMeta {
    pub bucket: Bucket,
    pub key: Key,
    pub digest: Digest,
    pub size: u64,
    pub etag: ETag,
    pub content_type: String,
    pub last_modified: DateTime<Utc>,
}
