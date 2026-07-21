//! S3 XML wire format — emit ListBucket / multipart / Error bodies, parse
//! `CompleteMultipartUpload`.
//!
//! The HTTP handlers in [`crate::routes`] speak this dialect so real clients
//! (`aws` CLI, Arrow's `object_store`) can deserialize responses without a
//! custom adapter. Encoding is hand-rolled (small fixed schemas); decoding uses
//! `quick-xml` + serde.
//!
//! Lifecycle configuration stays JSON (out of scope for the wire-format SPEC
//! item). [`parse_complete_multipart_body`] also accepts a playground JSON
//! body when `Content-Type` is `application/json`.

use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};
use serde::Deserialize;

use crate::error::AppError;
use crate::multipart::PartETag;
use crate::object::ETag;

/// One object row inside a `ListBucketResult` `<Contents>` element.
///
/// Built from the index's live version of a key before
/// [`list_bucket_result`] serialises it. Version ids are intentionally omitted —
/// standard ListObjectsV2 XML does not carry them; clients use `?versionId=` on
/// GET/HEAD/DELETE instead.
pub struct ListContent {
    /// Object key within the bucket (may contain `/`; the keyspace is flat).
    pub key: String,
    /// When this live version was written (UTC).
    pub last_modified: DateTime<Utc>,
    /// Hex ETag without surrounding quotes; emitters add S3-style quoting.
    pub etag: String,
    /// Object size in bytes.
    pub size: u64,
}

/// Inputs for a `ListObjectsV2` XML body.
///
/// Mirrors one page from [`crate::index::Index::list`] plus the query knobs the
/// client sent (`prefix`, `delimiter`, `max_keys`).
pub struct ListBucketParams<'a> {
    /// Bucket name (`<Name>`).
    pub name: &'a str,
    /// Listing prefix filter (`<Prefix>`), possibly empty.
    pub prefix: &'a str,
    /// Pseudo-directory delimiter when set (`<Delimiter>`).
    pub delimiter: Option<&'a str>,
    /// Page size cap echoed back as `<MaxKeys>`.
    pub max_keys: usize,
    /// Whether another page follows (`<IsTruncated>`).
    pub is_truncated: bool,
    /// Token to resume pagination (`<NextContinuationToken>`).
    pub next_continuation_token: Option<&'a str>,
    /// Live objects on this page (`<Contents>`).
    pub contents: &'a [ListContent],
    /// Rolled-up prefixes under `delimiter` (`<CommonPrefixes>`).
    pub common_prefixes: &'a [String],
}

/// Escape XML special characters in element text (keys, messages, tokens).
fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

/// Quote an ETag the way S3 does on the wire: `"abc123"` (or `"abc-2"`).
fn quoted_etag(etag: &str) -> String {
    let trimmed = etag.trim_matches('"');
    format!("\"{trimmed}\"")
}

/// ISO-8601 UTC timestamp S3 uses inside XML (`LastModified`).
fn iso8601(dt: DateTime<Utc>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S.000Z").to_string()
}

/// Build an HTTP response with `Content-Type: application/xml`.
///
/// Used for every S3 XML success and error body so SDK parsers pick the right
/// deserializer (they will not accept JSON for ListBucket / multipart).
pub fn xml_response(status: StatusCode, body: String) -> Response {
    let mut res = (status, body).into_response();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/xml"),
    );
    res
}

/// Serialise a ListObjectsV2 page as `<ListBucketResult xmlns="…">…</ListBucketResult>`.
///
/// ETags are emitted quoted (`"hex"`); text fields are XML-escaped. Omits
/// `<Delimiter>` and `<NextContinuationToken>` when those inputs are `None`.
pub fn list_bucket_result(p: &ListBucketParams<'_>) -> String {
    let mut xml = String::from(
        r#"<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">"#,
    );
    xml.push_str(&format!("<Name>{}</Name>", escape(p.name)));
    xml.push_str(&format!("<Prefix>{}</Prefix>", escape(p.prefix)));
    if let Some(delim) = p.delimiter {
        xml.push_str(&format!("<Delimiter>{}</Delimiter>", escape(delim)));
    }
    xml.push_str(&format!("<MaxKeys>{}</MaxKeys>", p.max_keys));
    xml.push_str(&format!(
        "<IsTruncated>{}</IsTruncated>",
        if p.is_truncated { "true" } else { "false" }
    ));
    if let Some(token) = p.next_continuation_token {
        xml.push_str(&format!(
            "<NextContinuationToken>{}</NextContinuationToken>",
            escape(token)
        ));
    }
    for c in p.contents {
        xml.push_str("<Contents>");
        xml.push_str(&format!("<Key>{}</Key>", escape(&c.key)));
        xml.push_str(&format!(
            "<LastModified>{}</LastModified>",
            iso8601(c.last_modified)
        ));
        xml.push_str(&format!("<ETag>{}</ETag>", escape(&quoted_etag(&c.etag))));
        xml.push_str(&format!("<Size>{}</Size>", c.size));
        xml.push_str("<StorageClass>STANDARD</StorageClass>");
        xml.push_str("</Contents>");
    }
    for prefix in p.common_prefixes {
        xml.push_str("<CommonPrefixes>");
        xml.push_str(&format!("<Prefix>{}</Prefix>", escape(prefix)));
        xml.push_str("</CommonPrefixes>");
    }
    xml.push_str("</ListBucketResult>");
    xml
}

/// Serialise an InitiateMultipartUpload response body.
///
/// Clients echo the returned `UploadId` on every subsequent UploadPart /
/// Complete / Abort for the same session.
pub fn initiate_multipart_result(bucket: &str, key: &str, upload_id: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Bucket>{}</Bucket><Key>{}</Key><UploadId>{}</UploadId></InitiateMultipartUploadResult>"#,
        escape(bucket),
        escape(key),
        escape(upload_id),
    )
}

/// Serialise a CompleteMultipartUpload response body.
///
/// The `etag` is the assembled multipart ETag (often `hex-N`); it is quoted on
/// the wire the same way as single-PUT ETags.
pub fn complete_multipart_result(bucket: &str, key: &str, etag: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Bucket>{}</Bucket><Key>{}</Key><ETag>{}</ETag></CompleteMultipartUploadResult>"#,
        escape(bucket),
        escape(key),
        escape(&quoted_etag(etag)),
    )
}

/// Serialise an S3 `<Error><Code>…</Code><Message>…</Message></Error>` body.
///
/// Pair with [`error_code`] and [`xml_response`] from [`AppError`]'s
/// `IntoResponse` impl.
pub fn error_body(code: &str, message: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><Error><Code>{}</Code><Message>{}</Message></Error>"#,
        escape(code),
        escape(message),
    )
}

/// Map an [`AppError`] variant to the S3 error `Code` string clients key on.
///
/// I/O and opaque failures both become `InternalError` so internals are not
/// leaked in the XML envelope (the human message is already scrubbed for 5xx).
pub fn error_code(err: &AppError) -> &'static str {
    match err {
        AppError::NoSuchBucket => "NoSuchBucket",
        AppError::NoSuchKey => "NoSuchKey",
        AppError::NoSuchUpload => "NoSuchUpload",
        AppError::BucketAlreadyExists => "BucketAlreadyExists",
        AppError::InvalidRequest(_) => "InvalidRequest",
        AppError::EntityTooLarge => "EntityTooLarge",
        AppError::PreconditionFailed => "PreconditionFailed",
        AppError::AccessDenied => "AccessDenied",
        AppError::Io(_) | AppError::Other(_) => "InternalError",
    }
}

/// Serde shape of a `CompleteMultipartUpload` request body (S3 XML).
#[derive(Debug, Deserialize)]
struct CompleteMultipartUploadXml {
    #[serde(rename = "Part", default)]
    parts: Vec<CompletePartXml>,
}

#[derive(Debug, Deserialize)]
struct CompletePartXml {
    #[serde(rename = "PartNumber")]
    part_number: u32,
    #[serde(rename = "ETag")]
    etag: String,
}

/// Parse a S3 `CompleteMultipartUpload` XML body into [`PartETag`]s.
///
/// Strips surrounding quotes from each `ETag` (clients often send `"md5hex"`).
/// The resulting list is what [`crate::multipart::Multipart::complete`] verifies
/// against staged part digests.
///
/// # Errors
///
/// Returns [`AppError::InvalidRequest`] when the body is not well-formed XML or
/// does not match the expected element names.
pub fn parse_complete_multipart_xml(bytes: &[u8]) -> Result<Vec<PartETag>, AppError> {
    let parsed: CompleteMultipartUploadXml = quick_xml::de::from_reader(bytes).map_err(|e| {
        AppError::InvalidRequest(format!("invalid CompleteMultipartUpload XML: {e}"))
    })?;
    Ok(parsed
        .parts
        .into_iter()
        .map(|p| PartETag {
            part_number: p.part_number,
            etag: ETag(p.etag.trim_matches('"').to_string()),
        })
        .collect())
}

/// Playground JSON shape for CompleteMultipartUpload (Content-Type fallback).
#[derive(Debug, Deserialize)]
struct CompleteMultipartJson {
    parts: Vec<CompletePartJson>,
}

#[derive(Debug, Deserialize)]
struct CompletePartJson {
    #[serde(rename = "partNumber")]
    part_number: u32,
    etag: String,
}

/// Parse the playground's JSON complete body (`{ "parts": [...] }`).
///
/// Kept so the web console can finish multipart uploads without building S3 XML
/// in the browser; real SDKs use [`parse_complete_multipart_xml`] instead.
///
/// # Errors
///
/// Returns [`AppError::InvalidRequest`] when the JSON is missing or malformed.
pub fn parse_complete_multipart_json(bytes: &[u8]) -> Result<Vec<PartETag>, AppError> {
    let parsed: CompleteMultipartJson = serde_json::from_slice(bytes).map_err(|e| {
        AppError::InvalidRequest(format!("invalid CompleteMultipartUpload JSON: {e}"))
    })?;
    Ok(parsed
        .parts
        .into_iter()
        .map(|p| PartETag {
            part_number: p.part_number,
            etag: ETag(p.etag.trim_matches('"').to_string()),
        })
        .collect())
}

/// Dispatch CompleteMultipartUpload body parsing by `Content-Type`.
///
/// JSON is used only when the type is explicitly `application/json` (optionally
/// with parameters after `;`). Anything else — including missing Content-Type —
/// is treated as S3 XML, which is what AWS SDKs and Arrow's client send.
///
/// # Errors
///
/// Propagates [`AppError::InvalidRequest`] from the chosen parser.
pub fn parse_complete_multipart_body(
    content_type: Option<&str>,
    bytes: &[u8],
) -> Result<Vec<PartETag>, AppError> {
    let is_json = content_type.is_some_and(|ct| {
        let ct = ct.split(';').next().unwrap_or(ct).trim();
        ct.eq_ignore_ascii_case("application/json")
    });
    if is_json {
        parse_complete_multipart_json(bytes)
    } else {
        parse_complete_multipart_xml(bytes)
    }
}

/// Parsed S3 `<Error>` envelope (`Code` + `Message`).
///
/// Used by integration tests (and any client that wants structured errors) after
/// reading a non-2xx response body.
#[derive(Debug, Deserialize)]
pub struct ParsedError {
    /// S3 error code (e.g. `NoSuchKey`), matching [`error_code`].
    #[serde(rename = "Code")]
    pub code: String,
    /// Human-readable message from the server.
    #[serde(rename = "Message")]
    pub message: String,
}

/// Parse an `<Error><Code>…</Code><Message>…</Message></Error>` body.
///
/// # Errors
///
/// Returns [`AppError::InvalidRequest`] when the XML cannot be deserialized.
pub fn parse_error(bytes: &[u8]) -> Result<ParsedError, AppError> {
    quick_xml::de::from_reader(bytes)
        .map_err(|e| AppError::InvalidRequest(format!("invalid Error XML: {e}")))
}

/// One `<Contents>` row from a parsed [`ParsedListBucket`].
#[derive(Debug, Deserialize, Default)]
pub struct ParsedContent {
    /// Object key.
    #[serde(rename = "Key", default)]
    pub key: String,
    /// ETag as on the wire (often still quoted; callers may strip `"`).
    #[serde(rename = "ETag", default)]
    pub etag: String,
    /// Size in bytes.
    #[serde(rename = "Size", default)]
    pub size: u64,
}

/// One `<CommonPrefixes><Prefix>…</Prefix></CommonPrefixes>` entry.
#[derive(Debug, Deserialize, Default)]
struct ParsedCommonPrefix {
    #[serde(rename = "Prefix", default)]
    prefix: String,
}

/// Parsed `ListBucketResult` (subset used by tests and the web console).
///
/// Only the fields needed for assertions / UI listing are retained; unused S3
/// elements are ignored by serde.
#[derive(Debug, Deserialize, Default)]
pub struct ParsedListBucket {
    /// Objects on this page.
    #[serde(rename = "Contents", default)]
    pub contents: Vec<ParsedContent>,
    #[serde(rename = "CommonPrefixes", default)]
    common_prefixes: Vec<ParsedCommonPrefix>,
    /// Whether another page follows.
    #[serde(rename = "IsTruncated", default)]
    pub is_truncated: bool,
    /// Resume token when truncated.
    #[serde(rename = "NextContinuationToken")]
    pub next_continuation_token: Option<String>,
}

impl ParsedListBucket {
    /// Collect object keys from [`Self::contents`] in document order.
    pub fn object_keys(&self) -> Vec<String> {
        self.contents.iter().map(|c| c.key.clone()).collect()
    }

    /// Collect common-prefix strings (the rolled-up "folders").
    pub fn common_prefix_strings(&self) -> Vec<String> {
        self.common_prefixes
            .iter()
            .map(|p| p.prefix.clone())
            .collect()
    }
}

/// Parse a `ListBucketResult` XML body into [`ParsedListBucket`].
///
/// # Errors
///
/// Returns [`AppError::InvalidRequest`] when the XML cannot be deserialized.
pub fn parse_list_bucket(bytes: &[u8]) -> Result<ParsedListBucket, AppError> {
    quick_xml::de::from_reader(bytes)
        .map_err(|e| AppError::InvalidRequest(format!("invalid ListBucketResult XML: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn list_bucket_result_includes_contents_and_prefixes() {
        let ts = Utc.with_ymd_and_hms(2024, 1, 2, 3, 4, 5).unwrap();
        let prefixes = vec!["a/b/".to_string()];
        let contents = vec![ListContent {
            key: "a/leaf.txt".into(),
            last_modified: ts,
            etag: "deadbeef".into(),
            size: 12,
        }];
        let xml = list_bucket_result(&ListBucketParams {
            name: "photos",
            prefix: "a/",
            delimiter: Some("/"),
            max_keys: 1000,
            is_truncated: false,
            next_continuation_token: None,
            contents: &contents,
            common_prefixes: &prefixes,
        });
        assert!(xml.contains("<Name>photos</Name>"));
        assert!(xml.contains("<Key>a/leaf.txt</Key>"));
        assert!(xml.contains("<ETag>&quot;deadbeef&quot;</ETag>"));
        assert!(xml.contains("<Prefix>a/b/</Prefix>"));
        assert!(xml.contains("<IsTruncated>false</IsTruncated>"));
    }

    #[test]
    fn parse_complete_multipart_xml_strips_etag_quotes() {
        let body = br#"<?xml version="1.0"?>
            <CompleteMultipartUpload>
              <Part><PartNumber>1</PartNumber><ETag>"abc"</ETag></Part>
              <Part><PartNumber>2</PartNumber><ETag>def</ETag></Part>
            </CompleteMultipartUpload>"#;
        let parts = parse_complete_multipart_xml(body).expect("parse");
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0].part_number, 1);
        assert_eq!(parts[0].etag.as_str(), "abc");
        assert_eq!(parts[1].etag.as_str(), "def");
    }

    #[test]
    fn parse_complete_multipart_json_round_trips() {
        let body = br#"{"parts":[{"partNumber":1,"etag":"\"aa\""}]}"#;
        let parts = parse_complete_multipart_json(body).expect("parse");
        assert_eq!(parts[0].part_number, 1);
        assert_eq!(parts[0].etag.as_str(), "aa");
    }

    #[test]
    fn escape_xml_specials() {
        assert_eq!(escape(r#"a&b<c>"d'e"#), "a&amp;b&lt;c&gt;&quot;d&apos;e");
    }
}
