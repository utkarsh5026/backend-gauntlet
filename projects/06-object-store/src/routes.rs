//! HTTP surface: the path-style S3 API.
//!
//! The routing, body streaming, and query-param dispatch are wired; what the
//! handlers call into — `streaming::stream_to_store`, `index.*`, `multipart.*` —
//! is where the `todo!()`s live. Run as-is and `GET /healthz` works; the first
//! real PUT/GET/list panics with a V1/V2/V3 todo, which is the worklist.
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
use crate::object::{ETag, ObjectMeta};
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
        "objects": listing.objects.iter().map(|o| json!({
            "key": o.key,
            "size": o.size,
            "etag": o.etag.0,
            "lastModified": o.last_modified,
        })).collect::<Vec<_>>(),
        "commonPrefixes": listing.common_prefixes,
        "isTruncated": listing.next_continuation_token.is_some(),
        "nextContinuationToken": listing.next_continuation_token,
    });
    Ok(Json(body).into_response())
}

/// `PUT /{bucket}/{key}` — store an object, OR upload a multipart part when
/// `?uploadId&partNumber` are present (V2 / V4). The body is streamed either way.
///
/// TODO(security): authenticate this and guard against path traversal in `key`
/// before doing anything — an open PUT is an open disk for the whole internet.
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
    body: Body,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);

    let _guard = crate::metrics::InFlightGuard::new();
    let content_type = content_type_of(&headers);
    let stream = body.into_data_stream();

    if let (Some(upload_id), Some(part_number)) = (q.upload_id.as_deref(), q.part_number) {
        let part = state
            .multipart
            .upload_part(upload_id, part_number, stream, state.max_object_size)
            .await?;
        return Ok((StatusCode::OK, [(header::ETAG, part.etag.0)]).into_response());
    }

    let stored = streaming::stream_to_store(&state.store, stream, state.max_object_size).await?;
    let meta = ObjectMeta {
        bucket,
        key,
        digest: stored.digest,
        size: stored.size,
        etag: stored.etag.clone(),
        content_type,
        last_modified: Utc::now(),
    };
    state.index.put(meta).await?;
    span.record(SIZE_KEY, stored.size);
    Ok((StatusCode::OK, [(header::ETAG, stored.etag.0)]).into_response())
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
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);

    let meta = require_object(&state.index, &bucket, &key, &span).await?;
    if let Some(response) = check_etag_present(&headers, &meta.etag)? {
        return Ok(response);
    }

    let is_range = headers.contains_key(header::RANGE);
    let (start, end) = if let Some(range) = headers.get(header::RANGE) {
        let range = range
            .to_str()
            .map_err(|_| invalid_err("Range header is not valid UTF-8"))?;

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
        (start, end)
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
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(&bucket, &key);
    let meta = require_object(&state.index, &bucket, &key, &span).await?;

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
/// AbortMultipartUpload.
#[derive(Debug, Deserialize)]
struct DeleteQuery {
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
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
    if let Some(meta) = state.index.get(&bucket, &key).await? {
        span.record(SIZE_KEY, meta.size);
    }
    state.index.delete(&bucket, &key).await?;
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
        return Ok(Json(json!({
            BUCKET_KEY: meta.bucket,
            KEY: meta.key,
            "etag": meta.etag.0,
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
    span: &tracing::Span,
) -> Result<ObjectMeta, AppError> {
    let meta = index.get(bucket, key).await?.ok_or(AppError::NoSuchKey)?;
    span.record(SIZE_KEY, meta.size);
    Ok(meta)
}

/// Returns `304 Not Modified` when the request's `If-None-Match` matches `etag`.
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
