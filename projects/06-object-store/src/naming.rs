//! S3-style bucket and key naming — shared path-safety rules.
//!
//! Any module that turns `(bucket, key)` into filesystem paths (`index`,
//! `multipart`, …) should use these helpers so naming rules stay in one place.

use crate::error::AppError;

/// Enforce S3-style bucket naming (3–63 chars, `[a-z0-9-]`, no leading or
/// trailing hyphen).
///
/// Doubles as the path-traversal defense: because `/`, `.`, and `_` are
/// rejected, a validated bucket name can only ever be a single directory
/// segment under the index root.
///
/// # Errors
///
/// [`AppError::InvalidRequest`] describing the first rule the name breaks.
pub fn validate_bucket_name(bucket: &str) -> Result<(), AppError> {
    if bucket.len() < 3 || bucket.len() > 63 {
        return Err(AppError::InvalidRequest(
            "bucket name must be 3–63 characters".into(),
        ));
    }
    if !bucket
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    {
        return Err(AppError::InvalidRequest(
            "bucket name may only contain lowercase letters, digits, and hyphens".into(),
        ));
    }

    if bucket.starts_with('-') || bucket.ends_with('-') {
        return Err(AppError::InvalidRequest(
            "bucket name may not start or end with a hyphen".into(),
        ));
    }

    Ok(())
}

/// Percent-encode a key into a single safe filename component.
///
/// The keyspace is flat, so `a/b/c.jpg` must become one file, not a nested
/// directory. Unreserved characters (`[A-Za-z0-9-._~]`) pass through; every
/// other byte becomes `%XX`, which flattens `/` and neutralizes any
/// path-traversal attempt in the key.
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
    use super::{encode_key, validate_bucket_name};
    use crate::error::AppError;

    #[test]
    fn validate_bucket_name_accepts_valid_names() {
        let max_len = "a".repeat(63);
        for name in ["abc", "photos", "my-bucket", "a1b2c3", max_len.as_str()] {
            validate_bucket_name(name).unwrap_or_else(|e| {
                panic!("{name:?} should be a valid bucket name, got {e}");
            });
        }
    }

    #[test]
    fn validate_bucket_name_rejects_length_outside_bounds() {
        assert!(
            matches!(validate_bucket_name(""), Err(AppError::InvalidRequest(_))),
            "empty is below the 3-char minimum"
        );
        assert!(
            matches!(validate_bucket_name("ab"), Err(AppError::InvalidRequest(_))),
            "2 chars is below the 3-char minimum"
        );
        assert!(
            matches!(
                validate_bucket_name(&"a".repeat(64)),
                Err(AppError::InvalidRequest(_))
            ),
            "64 chars is above the 63-char maximum"
        );
    }

    /// The charset whitelist is also the path-traversal defense: `/`, `.`, `_`,
    /// and uppercase can never name a bucket directory segment.
    #[test]
    fn validate_bucket_name_rejects_illegal_chars() {
        for bad in ["Photos", "my_bucket", "a/b", "../etc", "my.bucket", "café"] {
            assert!(
                matches!(validate_bucket_name(bad), Err(AppError::InvalidRequest(_))),
                "{bad:?} must be rejected as an invalid bucket name"
            );
        }
    }

    #[test]
    fn validate_bucket_name_rejects_leading_or_trailing_hyphen() {
        assert!(
            matches!(
                validate_bucket_name("-photos"),
                Err(AppError::InvalidRequest(_))
            ),
            "a leading hyphen is not a valid S3 bucket name"
        );
        assert!(
            matches!(
                validate_bucket_name("photos-"),
                Err(AppError::InvalidRequest(_))
            ),
            "a trailing hyphen is not a valid S3 bucket name"
        );
        assert!(
            matches!(validate_bucket_name("-"), Err(AppError::InvalidRequest(_))),
            "a lone hyphen fails both length and hyphen-edge rules"
        );
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

    /// `/` becomes `%2f`, so a traversal-shaped key collapses to one filename
    /// component and cannot climb out of the bucket directory.
    #[test]
    fn encode_key_neutralizes_path_traversal() {
        assert_eq!(encode_key("../../etc/passwd"), "..%2f..%2fetc%2fpasswd");
        assert!(!encode_key("../../etc/passwd").contains('/'));
    }

    #[test]
    fn encode_key_encodes_non_ascii_bytes() {
        // "café" in UTF-8 is c a f c3 a9 — the non-ASCII bytes become %XX.
        assert_eq!(encode_key("café"), "caf%c3%a9");
    }
}
