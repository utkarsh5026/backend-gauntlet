//! S3-style bucket and key naming — validated newtypes + path encoding.
//!
//! Construct [`Bucket`] / [`Key`] at the trust boundary (`Bucket::new` /
//! [`Key::new`]). Downstream code takes `&Bucket` / `&Key` (or `.as_str()`) and
//! does **not** re-run validation — the type is the proof.

use std::fmt;
use std::ops::Deref;

use axum::extract::{FromRequestParts, Path};
use axum::http::request::Parts;
use serde::{Deserialize, Serialize};

use crate::error::AppError;

/// A validated S3 bucket name (3–63 chars, `[a-z0-9-]`, no leading/trailing `-`).
///
/// Doubles as the path-traversal defense: `/`, `.`, and `_` are rejected, so a
/// [`Bucket`] can only ever be a single directory segment under the index root.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Bucket(String);

impl Bucket {
    /// Check S3-style bucket naming without constructing a [`Bucket`].
    ///
    /// Rules: 3–63 chars, `[a-z0-9-]`, no leading/trailing hyphen.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] describing the first rule the name breaks.
    pub fn validate(name: &str) -> Result<(), AppError> {
        if name.len() < 3 || name.len() > 63 {
            return Err(AppError::InvalidRequest(
                "bucket name must be 3–63 characters".into(),
            ));
        }
        if !name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        {
            return Err(AppError::InvalidRequest(
                "bucket name may only contain lowercase letters, digits, and hyphens".into(),
            ));
        }

        if name.starts_with('-') || name.ends_with('-') {
            return Err(AppError::InvalidRequest(
                "bucket name may not start or end with a hyphen".into(),
            ));
        }

        Ok(())
    }

    /// Validate and wrap a bucket name.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] describing the first rule the name breaks.
    pub fn new(name: impl Into<String>) -> Result<Self, AppError> {
        let name = name.into();
        Self::validate(&name)?;
        Ok(Self(name))
    }

    /// Wrap a name that is already known to be valid (e.g. loaded from our own
    /// index layout after a prior [`Self::new`]). Does not re-validate.
    pub fn from_trusted(name: impl Into<String>) -> Self {
        Self(name.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn into_string(self) -> String {
        self.0
    }
}

impl Deref for Bucket {
    type Target = str;
    fn deref(&self) -> &str {
        &self.0
    }
}

impl AsRef<str> for Bucket {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

impl PartialEq<str> for Bucket {
    fn eq(&self, other: &str) -> bool {
        self.0 == other
    }
}

impl PartialEq<&str> for Bucket {
    fn eq(&self, other: &&str) -> bool {
        self.0 == *other
    }
}

impl fmt::Display for Bucket {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl TryFrom<String> for Bucket {
    type Error = AppError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

impl TryFrom<&str> for Bucket {
    type Error = AppError;
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

impl<S: Send + Sync> FromRequestParts<S> for Bucket {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let Path(raw) = Path::<String>::from_request_parts(parts, state)
            .await
            .map_err(|e| AppError::InvalidRequest(e.to_string()))?;
        Self::new(raw)
    }
}

/// A validated object key: non-empty, at most 1024 UTF-8 bytes.
///
/// The keyspace is flat — `/` is allowed and only meaningful to listing, never
/// a real directory. Use [`Key::encode`] before joining into a filesystem path.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Key(String);

impl Key {
    /// Check key length rules without constructing a [`Key`].
    ///
    /// S3 allows keys up to **1024 UTF-8 bytes** and disallows the empty key.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] if the key is empty or exceeds 1024 bytes.
    pub fn validate(key: &str) -> Result<(), AppError> {
        const MAX_KEY_LEN: usize = 1024;
        if key.is_empty() {
            return Err(AppError::InvalidRequest(
                "object key must not be empty".into(),
            ));
        }
        if key.len() > MAX_KEY_LEN {
            return Err(AppError::InvalidRequest(format!(
                "object key must be at most {MAX_KEY_LEN} bytes, got {}",
                key.len()
            )));
        }
        Ok(())
    }

    /// Validate and wrap an object key.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] if the key is empty or exceeds 1024 bytes.
    pub fn new(key: impl Into<String>) -> Result<Self, AppError> {
        let key = key.into();
        Self::validate(&key)?;
        Ok(Self(key))
    }

    /// Wrap a key already known to be valid (e.g. deserialized from our index).
    pub fn from_trusted(key: impl Into<String>) -> Self {
        Self(key.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn into_string(self) -> String {
        self.0
    }

    /// Percent-encode into a single safe filename component (see [`encode_key`]).
    pub fn encode(&self) -> String {
        encode_key(&self.0)
    }
}

impl Deref for Key {
    type Target = str;
    fn deref(&self) -> &str {
        &self.0
    }
}

impl AsRef<str> for Key {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

impl PartialEq<str> for Key {
    fn eq(&self, other: &str) -> bool {
        self.0 == other
    }
}

impl PartialEq<&str> for Key {
    fn eq(&self, other: &&str) -> bool {
        self.0 == *other
    }
}

impl fmt::Display for Key {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl TryFrom<String> for Key {
    type Error = AppError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

impl TryFrom<&str> for Key {
    type Error = AppError;
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

/// Validated `(bucket, key)` pair — the usual object address.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ObjectPath {
    pub bucket: Bucket,
    pub key: Key,
}

impl ObjectPath {
    /// Validate both sides.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] if either name fails its rules.
    pub fn new(bucket: impl Into<String>, key: impl Into<String>) -> Result<Self, AppError> {
        Ok(Self {
            bucket: Bucket::new(bucket)?,
            key: Key::new(key)?,
        })
    }
}

impl<S: Send + Sync> FromRequestParts<S> for ObjectPath {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let Path((bucket, key)) = Path::<(String, String)>::from_request_parts(parts, state)
            .await
            .map_err(|e| AppError::InvalidRequest(e.to_string()))?;
        Self::new(bucket, key)
    }
}

/// Percent-encode a key into a single safe filename component.
///
/// Prefer [`Key::encode`] when you already hold a [`Key`].
pub fn encode_key(key: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(key.len());
    for byte in key.bytes() {
        match byte {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(byte as char);
            }
            _ => {
                encoded.push('%');
                encoded.push(HEX[(byte >> 4) as usize] as char);
                encoded.push(HEX[(byte & 0xf) as usize] as char);
            }
        }
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::AppError;

    #[test]
    fn bucket_new_accepts_valid_names() {
        let max_len = "a".repeat(63);
        for name in ["abc", "photos", "my-bucket", "a1b2c3", max_len.as_str()] {
            Bucket::new(name).unwrap_or_else(|e| {
                panic!("{name:?} should be a valid bucket name, got {e}");
            });
        }
    }

    #[test]
    fn bucket_new_rejects_length_outside_bounds() {
        assert!(matches!(Bucket::new(""), Err(AppError::InvalidRequest(_))));
        assert!(matches!(
            Bucket::new("ab"),
            Err(AppError::InvalidRequest(_))
        ));
        assert!(matches!(
            Bucket::new("a".repeat(64)),
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[test]
    fn bucket_new_rejects_illegal_chars() {
        for bad in ["Photos", "my_bucket", "a/b", "../etc", "my.bucket", "café"] {
            assert!(
                matches!(Bucket::new(bad), Err(AppError::InvalidRequest(_))),
                "{bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn bucket_new_rejects_leading_or_trailing_hyphen() {
        assert!(matches!(
            Bucket::new("-photos"),
            Err(AppError::InvalidRequest(_))
        ));
        assert!(matches!(
            Bucket::new("photos-"),
            Err(AppError::InvalidRequest(_))
        ));
        assert!(matches!(Bucket::new("-"), Err(AppError::InvalidRequest(_))));
    }

    #[test]
    fn encode_key_passes_unreserved_chars_through() {
        assert_eq!(encode_key("beach.jpg"), "beach.jpg");
        assert_eq!(encode_key("A-Za-z0-9._~"), "A-Za-z0-9._~");
        assert_eq!(encode_key(""), "");
    }

    #[test]
    fn encode_key_percent_encodes_slashes_and_spaces() {
        assert_eq!(encode_key("vacation/beach.jpg"), "vacation%2fbeach.jpg");
        assert_eq!(encode_key("a/b/c.jpg"), "a%2fb%2fc.jpg");
        assert_eq!(encode_key("my file.txt"), "my%20file.txt");
    }

    #[test]
    fn encode_key_neutralizes_path_traversal() {
        assert_eq!(encode_key("../../etc/passwd"), "..%2f..%2fetc%2fpasswd");
        assert!(!encode_key("../../etc/passwd").contains('/'));
    }

    #[test]
    fn encode_key_encodes_non_ascii_bytes() {
        assert_eq!(encode_key("café"), "caf%c3%a9");
    }

    #[test]
    fn key_new_accepts_normal_and_max_length_keys() {
        for key in [
            "a",
            "a/b/c.jpg",
            "with spaces & symbols!",
            "../../etc/passwd",
        ] {
            Key::new(key).unwrap_or_else(|e| panic!("{key:?} should be valid, got {e}"));
        }
        Key::new("k".repeat(1024)).expect("1024-byte key at the cap");
    }

    #[test]
    fn key_new_rejects_empty_and_over_length() {
        assert!(matches!(Key::new(""), Err(AppError::InvalidRequest(_))));
        assert!(matches!(
            Key::new("k".repeat(1025)),
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[test]
    fn key_new_counts_bytes_not_chars() {
        let key = "é".repeat(513);
        assert_eq!(key.chars().count(), 513);
        assert!(matches!(Key::new(key), Err(AppError::InvalidRequest(_))));
    }

    #[test]
    fn object_path_validates_both_sides() {
        let path = ObjectPath::new("photos", "a/b.txt").unwrap();
        assert_eq!(path.bucket.as_str(), "photos");
        assert_eq!(path.key.as_str(), "a/b.txt");
        assert!(ObjectPath::new("Bad", "k").is_err());
        assert!(ObjectPath::new("photos", "").is_err());
    }
}
