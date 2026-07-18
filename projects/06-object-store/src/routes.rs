//! HTTP surface: the path-style S3 API.
//!
//! Path-style routing, body streaming, query-param dispatch (including
//! `?versionId=`), and the `AppError →` status mapping live here. The vertical
//! meat sits in `streaming` / `index` / `multipart` / `store`.
//!
//! S3 multipart reuses the object routes and dispatches on query params:
//!   - `POST   /{bucket}/{key}?uploads`                 → InitiateMultipartUpload
//!   - `PUT    /{bucket}/{key}?uploadId=…&partNumber=N` → UploadPart
//!   - `POST   /{bucket}/{key}?uploadId=…`              → CompleteMultipartUpload
//!   - `DELETE /{bucket}/{key}?uploadId=…`              → AbortMultipartUpload

use axum::body::Body;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, put};
use axum::{Json, Router};
use chrono::{DateTime, Utc};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::Deserialize;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::index::Index;
use crate::naming::validate_key;
use crate::object::{ETag, ObjectRef, ResolvedObject};
use crate::streaming::ChecksumSpec;
use crate::{streaming, AppState};

const BUCKET_KEY: &str = "bucket";
const KEY: &str = "key";
const SIZE_KEY: &str = "size";

/// Build the path-style S3 API router over the given [`AppState`].
///
/// Wires bucket routes (`/{bucket}`) and object routes (`/{bucket}/{*key}`),
/// with the object POST/PUT/DELETE verbs doubling as the query-dispatched
/// multipart API (see the module docs). The body limit is disabled so large
/// uploads stream through [`streaming::stream_to_store`] rather than being
/// buffered, and every request is traced via [`TraceLayer`]. `GET /metrics`
/// lives in the separate [`metrics_router`].
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/{bucket}", put(create_bucket).get(list_objects))
        // Object-level: put/get/delete + the multipart verbs (query-dispatched).
        .route(
            "/{bucket}/{*key}",
            put(put_object)
                .get(get_object)
                .head(head_object)
                .delete(delete_object)
                .post(post_object),
        )
        .layer(DefaultBodyLimit::disable())
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

/// A standalone router exposing `GET /metrics` in Prometheus exposition format.
///
/// Kept separate from [`router`] (and merged in by `main`) so the integration
/// tests can build the API surface without installing a process-global recorder
/// — the metric call sites are no-ops until [`crate::metrics::install`] runs.
pub fn metrics_router(handle: PrometheusHandle) -> Router {
    Router::new().route(
        "/metrics",
        get(move || {
            let handle = handle.clone();
            async move { handle.render() }
        }),
    )
}

async fn healthz() -> &'static str {
    "ok"
}

/// `PUT /{bucket}` — create a bucket (V3 `CreateBucket`).
///
/// # Errors
///
/// Returns an [`AppError`] if the index rejects the bucket (e.g. an invalid
/// name, or a storage failure).
async fn create_bucket(
    State(state): State<AppState>,
    Path(bucket): Path<String>,
) -> Result<StatusCode, AppError> {
    state.index.create_bucket(&bucket).await?;
    Ok(StatusCode::OK)
}

/// `GET /{bucket}` — list objects (V3 `ListObjectsV2`).
///
/// TODO(protocol): S3 returns a `<ListBucketResult>` XML body; we emit JSON as a
/// placeholder. Switch to XML for real `aws s3` / SDK compatibility.
async fn list_objects(
    State(state): State<AppState>,
    Path(bucket): Path<String>,
    Query(q): Query<ListQuery>,
) -> Result<Response, AppError> {
    let listing = state
        .index
        .list(
            &bucket,
            q.prefix.as_deref().unwrap_or(""),
            q.delimiter.as_deref(),
            q.continuation_token.as_deref(),
            q.max_keys.unwrap_or(1000),
        )
        .await?;

    let body = json!({
        "name": bucket,
        "prefix": q.prefix.unwrap_or_default(),
        "objects": listing.objects.iter().filter_map(|o| {
            let live = o.latest_live()?;
            Some(json!({
                "key": o.key,
                "size": live.size,
                "etag": live.etag.0,
                "lastModified": live.last_modified,
                "versionId": live.version_id,
            }))
        }).collect::<Vec<_>>(),
        "commonPrefixes": listing.common_prefixes,
        "isTruncated": listing.next_continuation_token.is_some(),
        "nextContinuationToken": listing.next_continuation_token,
    });
    Ok(Json(body).into_response())
}

/// `PUT /{bucket}/{key}` — store an object, OR upload a multipart part when
/// `?uploadId&partNumber` are present (V2 / V4). The body is streamed either way.
///
/// Validates the key (length cap) before streaming, and stores the body through
/// the content-addressed layout — never a raw user path — so a traversal-shaped
/// key can't escape the data dir.
///
/// A `Content-MD5` or `X-Amz-Checksum-*` header opts the upload into checksum
/// verification; the three-header negotiation lives in [`ChecksumSpec`], so the
/// handler only ever sees the parsed result.
///
/// TODO(security): still unauthenticated — an open PUT is an open disk for the
/// whole internet. Gate writes behind a credential (SigV4, or a simpler HMAC).
#[tracing::instrument(
    skip_all,
    fields(
        bucket = tracing::field::Empty,
        key = tracing::field::Empty,
        size = tracing::field::Empty,
    )
)]
async fn put_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<PutQuery>,
    headers: HeaderMap,
    ChecksumSpec(checksum_algo): ChecksumSpec,
    body: Body,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);

    let _guard = crate::metrics::InFlightGuard::new();
    let content_type = content_type_of(&headers);
    let stream = body.into_data_stream();

    if let Some(if_match) = headers.get(header::IF_MATCH) {
        let index = state.index.get(&bucket, &key).await?;
        if let Some(index) = index {
            let live = index.latest_live().ok_or(AppError::PreconditionFailed)?;
            if live.etag.as_str() != if_match.to_str().unwrap() {
                return Err(AppError::PreconditionFailed);
            }
        } else {
            return Err(AppError::PreconditionFailed);
        }
    }

    if let (Some(upload_id), Some(part_number)) = (q.upload_id.as_deref(), q.part_number) {
        let part = state
            .multipart
            .upload_part(upload_id, part_number, stream, state.max_object_size)
            .await?;
        return Ok((StatusCode::OK, [(header::ETAG, part.etag.0)]).into_response());
    }

    validate_key(&key)?;

    let streaming::Stored { digest, etag, size } =
        streaming::stream_to_store(&state.store, stream, state.max_object_size, checksum_algo)
            .await?;

    state
        .index
        .put(&bucket, &key, digest, etag.clone(), size, content_type)
        .await?;
    span.record(SIZE_KEY, size);
    Ok((StatusCode::OK, [(header::ETAG, etag.0)]).into_response())
}

/// `GET /{bucket}/{key}` — stream an object back (V2). Fully wired: it looks up
/// the metadata (V3), opens the blob (V1), and streams the file as the body so
/// the bytes never all sit in memory at once.
///
/// Honours a `Range: bytes=a-b` header → `206 Partial Content` + `Content-Range`
/// (serving just that slice), and `If-None-Match` on the ETag → `304 Not
/// Modified`. Every full response carries `ETag`, `Content-Length`,
/// `Content-Type`, and `Last-Modified`; [`head_object`] returns the same headers
/// with no body.
#[tracing::instrument(
    skip_all,
    fields(
        bucket = tracing::field::Empty,
        key = tracing::field::Empty,
        size = tracing::field::Empty,
    )
)]
async fn get_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<GetQuery>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);
    let object_ref = ObjectRef::from_query(q.version_id);

    let meta = require_object(&state.index, &bucket, &key, object_ref, &span).await?;
    if let Some(response) = check_etag_present(&headers, &meta.etag)? {
        return Ok(response);
    }

    let is_range = headers.contains_key(header::RANGE);
    let (start, end) = if let Some(range) = headers.get(header::RANGE) {
        let range = range
            .to_str()
            .map_err(|_| invalid_err("Range header is not valid UTF-8"))?;

        validate_range(range)?
    } else {
        (0, meta.size.saturating_sub(1))
    };

    let file = state
        .store
        .open_blob_range(&meta.digest, start, end)
        .await?;
    let stream = tokio_util::io::ReaderStream::new(crate::metrics::ObservedDownload::new(file));
    let body = Body::from_stream(stream);
    let response_size = end - start + 1;

    let status = if is_range {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };

    let last_modified = http_date(meta.last_modified);
    let mut response = (
        status,
        [
            (header::CONTENT_TYPE, meta.content_type),
            (header::ETAG, meta.etag.0),
            (header::CONTENT_LENGTH, response_size.to_string()),
            (header::LAST_MODIFIED, last_modified),
        ],
        body,
    )
        .into_response();
    if is_range {
        response.headers_mut().insert(
            header::CONTENT_RANGE,
            format!("bytes {start}-{end}/{}", meta.size)
                .parse()
                .expect("numeric Content-Range is a valid header value"),
        );
        metrics::counter!(crate::metrics::RANGE_REQUESTS_SERVED_TOTAL).increment(1);
    }
    metrics::counter!(crate::metrics::OBJECTS_GET_TOTAL).increment(1);
    Ok(response)
}

/// Parse an HTTP `Range` header value into inclusive `(start, end)` byte
/// offsets.
///
/// Accepts only the form S3/`GetObject` needs here: `bytes=<start>-<end>`,
/// with both ends required (no open-ended `bytes=N-` / `bytes=-N` suffixes).
/// The unit must be `bytes`; anything else is [`AppError::InvalidRequest`].
/// Bounds are not checked against object size — [`crate::store::Store::open_blob_range`]
/// rejects an out-of-range slice after the blob is known.
fn validate_range(range: &str) -> Result<(u64, u64), AppError> {
    let Some((unit, bounds)) = range.split_once('=') else {
        return Err(invalid_err(format!(
            "Range header must be '<unit>=<start>-<end>', got {range:?}"
        )));
    };
    if unit != "bytes" {
        return Err(invalid_err(format!(
            "Range unit must be 'bytes', got {unit:?}"
        )));
    }

    let Some((start_str, end_str)) = bounds.split_once('-') else {
        return Err(invalid_err(format!(
            "Range bounds must be '<start>-<end>', got {bounds:?}"
        )));
    };

    let start = start_str.parse::<u64>().map_err(|_| {
        invalid_err(format!(
            "Range start must be a non-negative integer, got {start_str:?}"
        ))
    })?;
    let end = end_str.parse::<u64>().map_err(|_| {
        invalid_err(format!(
            "Range end must be a non-negative integer, got {end_str:?}"
        ))
    })?;
    Ok((start, end))
}

/// `HEAD /{bucket}/{key}` — the object's metadata headers with **no body**
/// (S3 `HeadObject`). Same lookup and conditional-request handling as GET, but it
/// deliberately never opens the blob: HEAD exists precisely so a client can read
/// `Content-Length`, `ETag`, `Content-Type`, and `Last-Modified` without paying to
/// transfer the bytes.
#[tracing::instrument(
    skip_all,
    fields(
        bucket = tracing::field::Empty,
        key = tracing::field::Empty,
        size = tracing::field::Empty,
    )
)]
async fn head_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<GetQuery>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);
    let object_ref = ObjectRef::from_query(q.version_id);
    let meta = require_object(&state.index, &bucket, &key, object_ref, &span).await?;

    if let Some(resp) = check_etag_present(&headers, &meta.etag)? {
        return Ok(resp);
    }

    let last_modified = http_date(meta.last_modified);
    Ok((
        [
            (header::CONTENT_TYPE, meta.content_type),
            (header::ETAG, meta.etag.0),
            (header::CONTENT_LENGTH, meta.size.to_string()),
            (header::LAST_MODIFIED, last_modified),
        ],
        Body::empty(),
    )
        .into_response())
}

/// Query params on `DELETE /{bucket}/{key}`; `?uploadId` switches delete to
/// AbortMultipartUpload. Optional `versionId` deletes one historical version.
#[derive(Debug, Deserialize)]
struct DeleteQuery {
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
    #[serde(rename = "versionId")]
    version_id: Option<u64>,
}

/// `DELETE /{bucket}/{key}` — delete an object (V3), OR abort a multipart
/// upload when `?uploadId` is present (V4 `AbortMultipartUpload`).
///
/// S3 delete is idempotent: removing an absent key still returns
/// `204 No Content`. The pre-delete `get` is only to record the object's size
/// on the span.
///
/// # Errors
///
/// Returns an [`AppError`] if aborting the upload or the index delete fails.
#[tracing::instrument(
    skip_all,
    fields(
        bucket = tracing::field::Empty,
        key = tracing::field::Empty,
        size = tracing::field::Empty,
    )
)]
async fn delete_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<DeleteQuery>,
) -> Result<StatusCode, AppError> {
    let span = make_span(&bucket, &key);
    if let Some(upload_id) = q.upload_id.as_deref() {
        state.multipart.abort(upload_id).await?;
        return Ok(StatusCode::NO_CONTENT);
    }
    let object_ref = ObjectRef::from_query(q.version_id);
    if let Ok(meta) = state.index.resolve(&bucket, &key, object_ref).await {
        span.record(SIZE_KEY, meta.size);
    }
    state.index.delete(&bucket, &key, object_ref).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// `POST /{bucket}/{key}` — the two body-less multipart verbs, dispatched on
/// query params (V4):
///   - `?uploads`     → InitiateMultipartUpload (returns a fresh `uploadId`).
///   - `?uploadId=…`  → CompleteMultipartUpload (assembles the uploaded parts).
///
/// A POST with neither param is a client error.
///
/// # Errors
///
/// Returns [`AppError::InvalidRequest`] for an unrecognised POST, or any error
/// bubbled up from `multipart.initiate` / `multipart.complete`.
async fn post_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<PostQuery>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, AppError> {
    if q.uploads.is_some() {
        let content_type = content_type_of(&headers);
        let upload_id = state
            .multipart
            .initiate(&bucket, &key, content_type)
            .await?;
        let response = json!({
            BUCKET_KEY: bucket,
            KEY: key,
            "uploadId": upload_id,
        });
        return Ok(Json(response).into_response());
    }

    // CompleteMultipartUpload: POST .../{key}?uploadId=…  (body lists the parts)
    if let Some(upload_id) = q.upload_id.as_deref() {
        // TODO(V4 / protocol): parse the CompleteMultipartUpload body into the
        // ordered (part_number, etag) list the client is committing, and pass it
        // to `complete`. The empty list below is a placeholder so this compiles.
        let _ = &body;
        let parts = Vec::new();
        let meta = state.multipart.complete(upload_id, parts).await?;
        let live = meta.latest_live().ok_or(AppError::NoSuchKey)?;
        return Ok(Json(json!({
            BUCKET_KEY: meta.bucket,
            KEY: meta.key,
            "etag": live.etag.0,
            "versionId": live.version_id,
        }))
        .into_response());
    }

    Err(AppError::InvalidRequest(
        "unrecognised POST (expected ?uploads or ?uploadId)".into(),
    ))
}

/// Format a timestamp as an HTTP-date (RFC 7231 IMF-fixdate), e.g.
/// `Tue, 15 Nov 1994 08:12:31 GMT` — the only format `Last-Modified` and
/// `If-Modified-Since` accept. The value is UTC, so the literal `GMT` is correct.
fn http_date(dt: DateTime<Utc>) -> String {
    dt.format("%a, %d %b %Y %H:%M:%S GMT").to_string()
}

/// Read the `Content-Type` header, defaulting to the S3 default for objects.
fn content_type_of(headers: &HeaderMap) -> String {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string()
}

fn invalid_err(msg: impl Into<String>) -> AppError {
    AppError::InvalidRequest(msg.into())
}

fn make_span(bucket: &str, key: &str) -> tracing::Span {
    let span = tracing::Span::current();
    span.record(BUCKET_KEY, bucket);
    span.record(KEY, key);
    span
}

async fn require_object(
    index: &Index,
    bucket: &str,
    key: &str,
    object_ref: ObjectRef,
    span: &tracing::Span,
) -> Result<ResolvedObject, AppError> {
    let meta = index.resolve(bucket, key, object_ref).await?;
    span.record(SIZE_KEY, meta.size);
    Ok(meta)
}

/// Conditional GET/HEAD helper for `If-None-Match`.
///
/// Returns `Some(304 Not Modified)` when the request's `If-None-Match` value
/// equals the object's [`ETag`] — the client already has the current bytes and
/// needs no body. Returns `Ok(None)` when the header is absent or does not
/// match, so the caller proceeds with a normal response. Non-UTF-8 header
/// values are [`AppError::InvalidRequest`].
fn check_etag_present(headers: &HeaderMap, etag: &ETag) -> Result<Option<Response>, AppError> {
    let Some(header_value) = headers.get(header::IF_NONE_MATCH) else {
        return Ok(None);
    };
    let if_none_match = header_value
        .to_str()
        .map_err(|_| AppError::InvalidRequest("invalid If-None-Match header".into()))?;
    if etag.as_str() == if_none_match {
        return Ok(Some(StatusCode::NOT_MODIFIED.into_response()));
    }
    Ok(None)
}

/// Query params on `PUT /{bucket}/{key}`. Both present ⇒ UploadPart; both
/// absent ⇒ a plain single-object PUT.
#[derive(Debug, Deserialize)]
struct PutQuery {
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
    #[serde(rename = "partNumber")]
    part_number: Option<u32>,
}

/// Query params on `POST /{bucket}/{key}` that select the multipart verb.
#[derive(Debug, Deserialize)]
struct PostQuery {
    /// Present (valueless `?uploads`) for InitiateMultipartUpload.
    uploads: Option<String>,
    /// Present for CompleteMultipartUpload.
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
}

/// Query params on `GET /{bucket}` (`ListObjectsV2`) — the standard S3 listing
/// knobs: prefix filter, `delimiter` for pseudo-directories, `continuation-token`
/// for paging, and `max-keys` to cap the page size.
#[derive(Debug, Deserialize)]
struct ListQuery {
    prefix: Option<String>,
    delimiter: Option<String>,
    #[serde(rename = "continuation-token")]
    continuation_token: Option<String>,
    #[serde(rename = "max-keys")]
    max_keys: Option<usize>,
}

/// Query params on `GET` / `HEAD /{bucket}/{key}` — optional `versionId` pins a
/// historical version; absent means latest.
#[derive(Debug, Deserialize)]
struct GetQuery {
    #[serde(rename = "versionId")]
    version_id: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{header, HeaderMap, HeaderValue, Request, StatusCode};
    use http_body_util::BodyExt;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn fresh_router() -> (TempDir, Router) {
        let dir = TempDir::new().expect("temp data dir");
        let state = AppState::open(dir.path(), 1 << 20).expect("open AppState");
        (dir, router(state))
    }

    async fn send(router: &Router, req: Request<Body>) -> (StatusCode, HeaderMap, bytes::Bytes) {
        let res = router
            .clone()
            .oneshot(req)
            .await
            .expect("router is infallible");
        let status = res.status();
        let headers = res.headers().clone();
        let body = res
            .into_body()
            .collect()
            .await
            .expect("collect body")
            .to_bytes();
        (status, headers, body)
    }

    async fn put_bytes(router: &Router, bucket: &str, key: &str, body: &[u8]) -> StatusCode {
        put_bytes_if_match(router, bucket, key, body, None)
            .await
            .0
    }

    async fn put_bytes_if_match(
        router: &Router,
        bucket: &str,
        key: &str,
        body: &[u8],
        if_match: Option<&str>,
    ) -> (StatusCode, HeaderMap, bytes::Bytes) {
        let mut req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}/{key}"))
            .header(header::CONTENT_TYPE, "text/plain");
        if let Some(etag) = if_match {
            req = req.header(header::IF_MATCH, etag);
        }
        send(router, req.body(Body::from(body.to_vec())).unwrap()).await
    }

    async fn ensure_bucket(router: &Router, bucket: &str) {
        let (status, _, _) = send(
            router,
            Request::builder()
                .method("PUT")
                .uri(format!("/{bucket}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }

    #[test]
    fn validate_range_parses_inclusive_byte_bounds() {
        assert_eq!(validate_range("bytes=0-0").unwrap(), (0, 0));
        assert_eq!(validate_range("bytes=2-5").unwrap(), (2, 5));
        assert_eq!(validate_range("bytes=9-9").unwrap(), (9, 9));
    }

    #[test]
    fn validate_range_rejects_malformed_values() {
        for bad in [
            "bytes",
            "items=0-1",
            "bytes=0",
            "bytes=abc-1",
            "bytes=1-xyz",
            "bytes=-",
        ] {
            assert!(
                matches!(validate_range(bad), Err(AppError::InvalidRequest(_))),
                "{bad:?} must be InvalidRequest"
            );
        }
    }

    #[test]
    fn content_type_of_defaults_when_header_absent() {
        let headers = HeaderMap::new();
        assert_eq!(content_type_of(&headers), "application/octet-stream");
    }

    #[test]
    fn content_type_of_reads_the_header() {
        let mut headers = HeaderMap::new();
        headers.insert(header::CONTENT_TYPE, HeaderValue::from_static("image/png"));
        assert_eq!(content_type_of(&headers), "image/png");
    }

    #[test]
    fn check_etag_present_is_none_without_header() {
        let headers = HeaderMap::new();
        let etag = ETag("abc".into());
        assert!(check_etag_present(&headers, &etag).unwrap().is_none());
    }

    #[test]
    fn check_etag_present_returns_304_on_match() {
        let mut headers = HeaderMap::new();
        headers.insert(header::IF_NONE_MATCH, HeaderValue::from_static("abc"));
        let etag = ETag("abc".into());
        let resp = check_etag_present(&headers, &etag)
            .unwrap()
            .expect("matching If-None-Match must short-circuit");
        assert_eq!(resp.status(), StatusCode::NOT_MODIFIED);
    }

    #[test]
    fn check_etag_present_falls_through_on_mismatch() {
        let mut headers = HeaderMap::new();
        headers.insert(header::IF_NONE_MATCH, HeaderValue::from_static("stale"));
        let etag = ETag("fresh".into());
        assert!(check_etag_present(&headers, &etag).unwrap().is_none());
    }

    #[test]
    fn http_date_formats_rfc7231_gmt() {
        let dt = DateTime::parse_from_rfc3339("1994-11-15T08:12:31Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(http_date(dt), "Tue, 15 Nov 1994 08:12:31 GMT");
    }

    #[test]
    fn get_query_absent_version_id_means_latest() {
        let q = GetQuery { version_id: None };
        assert_eq!(ObjectRef::from_query(q.version_id), ObjectRef::Latest);
    }

    #[test]
    fn get_query_version_id_pins_a_version() {
        let q = GetQuery {
            version_id: Some(3),
        };
        assert_eq!(ObjectRef::from_query(q.version_id), ObjectRef::Version(3));
    }

    #[test]
    fn delete_query_version_id_pins_a_version() {
        let q = DeleteQuery {
            upload_id: None,
            version_id: Some(1),
        };
        assert_eq!(ObjectRef::from_query(q.version_id), ObjectRef::Version(1));
    }

    // ── router: health + versionId dispatch ──────────────────────────────────

    #[tokio::test]
    async fn healthz_returns_ok() {
        let (_dir, router) = fresh_router();
        let req = Request::builder()
            .uri("/healthz")
            .body(Body::empty())
            .unwrap();
        let (status, _, body) = send(&router, req).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"ok");
    }

    #[tokio::test]
    async fn get_with_version_id_serves_that_version() {
        let (_dir, router) = fresh_router();
        assert_eq!(
            send(
                &router,
                Request::builder()
                    .method("PUT")
                    .uri("/docs")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .0,
            StatusCode::OK
        );

        assert_eq!(
            put_bytes(&router, "docs", "readme", b"first").await,
            StatusCode::OK
        );
        assert_eq!(
            put_bytes(&router, "docs", "readme", b"second").await,
            StatusCode::OK
        );

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/readme?versionId=1")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"first");

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/readme")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"second", "no versionId → latest");
    }

    #[tokio::test]
    async fn head_with_version_id_reports_that_versions_length() {
        let (_dir, router) = fresh_router();
        send(
            &router,
            Request::builder()
                .method("PUT")
                .uri("/docs")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        put_bytes(&router, "docs", "readme", b"ab").await;
        put_bytes(&router, "docs", "readme", b"abcdef").await;

        let (status, headers, body) = send(
            &router,
            Request::builder()
                .method("HEAD")
                .uri("/docs/readme?versionId=1")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.is_empty());
        assert_eq!(
            headers
                .get(header::CONTENT_LENGTH)
                .and_then(|v| v.to_str().ok()),
            Some("2"),
            "HEAD ?versionId=1 must report v1's size"
        );
    }

    #[tokio::test]
    async fn delete_with_version_id_removes_only_that_version() {
        let (_dir, router) = fresh_router();
        send(
            &router,
            Request::builder()
                .method("PUT")
                .uri("/docs")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        put_bytes(&router, "docs", "readme", b"keep").await;
        put_bytes(&router, "docs", "readme", b"drop").await;

        let (status, _, _) = send(
            &router,
            Request::builder()
                .method("DELETE")
                .uri("/docs/readme?versionId=2")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NO_CONTENT);

        let (status, _, _) = send(
            &router,
            Request::builder()
                .uri("/docs/readme?versionId=2")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/readme")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"keep");
    }

    #[tokio::test]
    async fn list_json_includes_latest_version_id() {
        let (_dir, router) = fresh_router();
        send(
            &router,
            Request::builder()
                .method("PUT")
                .uri("/docs")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        put_bytes(&router, "docs", "readme", b"v1").await;
        put_bytes(&router, "docs", "readme", b"v2").await;

        let (status, _, body) = send(
            &router,
            Request::builder().uri("/docs").body(Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let json: serde_json::Value = serde_json::from_slice(&body).expect("list JSON");
        let objects = json["objects"].as_array().expect("objects array");
        assert_eq!(objects.len(), 1);
        assert_eq!(objects[0]["key"], "readme");
        assert_eq!(objects[0]["versionId"], 2);
    }

    #[tokio::test]
    async fn post_without_multipart_params_is_400() {
        let (_dir, router) = fresh_router();
        ensure_bucket(&router, "docs").await;

        let (status, _, body) = send(
            &router,
            Request::builder()
                .method("POST")
                .uri("/docs/readme")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(
            json["error"]
                .as_str()
                .unwrap()
                .contains("unrecognised POST"),
            "bare POST must explain the missing multipart query"
        );
    }

    // ── Conditional writes (`If-Match` on PUT) ───────────────────────────────

    #[tokio::test]
    async fn put_if_match_with_current_etag_succeeds() {
        let (_dir, router) = fresh_router();
        ensure_bucket(&router, "docs").await;

        let (status, headers, _) = put_bytes_if_match(&router, "docs", "k", b"v1", None).await;
        assert_eq!(status, StatusCode::OK);
        let etag = headers
            .get(header::ETAG)
            .and_then(|v| v.to_str().ok())
            .expect("PUT returns ETag")
            .to_string();

        let (status, _, _) =
            put_bytes_if_match(&router, "docs", "k", b"v2", Some(&etag)).await;
        assert_eq!(status, StatusCode::OK);

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/k")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"v2");
    }

    #[tokio::test]
    async fn put_if_match_with_stale_etag_is_412() {
        let (_dir, router) = fresh_router();
        ensure_bucket(&router, "docs").await;

        let (status, headers, _) = put_bytes_if_match(&router, "docs", "k", b"v1", None).await;
        assert_eq!(status, StatusCode::OK);
        let stale = headers
            .get(header::ETAG)
            .and_then(|v| v.to_str().ok())
            .expect("PUT returns ETag")
            .to_string();

        assert_eq!(put_bytes(&router, "docs", "k", b"v2").await, StatusCode::OK);

        let (status, _, body) =
            put_bytes_if_match(&router, "docs", "k", b"lost", Some(&stale)).await;
        assert_eq!(status, StatusCode::PRECONDITION_FAILED);
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["error"], "precondition failed");

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/k")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"v2", "stale If-Match must not overwrite");
    }

    #[tokio::test]
    async fn put_if_match_on_missing_key_is_412() {
        let (_dir, router) = fresh_router();
        ensure_bucket(&router, "docs").await;

        let (status, _, _) =
            put_bytes_if_match(&router, "docs", "ghost", b"nope", Some("deadbeef")).await;
        assert_eq!(status, StatusCode::PRECONDITION_FAILED);

        let (status, _, _) = send(
            &router,
            Request::builder()
                .uri("/docs/ghost")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }
}
