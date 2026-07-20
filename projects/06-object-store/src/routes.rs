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

use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use chrono::{DateTime, Duration, Utc};
use metrics_exporter_prometheus::PrometheusHandle;
use serde::{Deserialize, Serialize};
use tower_http::trace::TraceLayer;

use crate::auth::{self, PresignRequest};
use crate::error::AppError;
use crate::index::Precondition;
use crate::index_backend::IndexBackend;
use crate::lifecycle::LifecyclePolicy;
use crate::naming::{Bucket, Key, ObjectPath};
use crate::object::{ETag, ObjectRef, ResolvedObject};
use crate::s3_xml::{
    complete_multipart_result, initiate_multipart_result, list_bucket_result,
    parse_complete_multipart_body, xml_response, ListBucketParams, ListContent,
};
use crate::streaming::ChecksumSpec;
use crate::{streaming, AppState};

const BUCKET_KEY: &str = "bucket";
const KEY: &str = "key";
const SIZE_KEY: &str = "size";

/// Build the path-style S3 API router over the given [`AppState`].
///
/// Wires bucket routes (`/{bucket}`) and object routes (`/{bucket}/{*key}`),
/// with the object POST/PUT/DELETE verbs doubling as the query-dispatched
/// multipart API (see the module docs). Object routes get
/// [`auth::object_auth_middleware`] via `route_layer` so handlers stay
/// auth-free. The body limit is disabled so large uploads stream through
/// [`streaming::stream_to_store`] rather than being buffered, and every
/// request is traced via [`TraceLayer`]. `GET /metrics` lives in the
/// separate [`metrics_router`].
pub fn router(state: AppState) -> Router {
    let object_routes = Router::new()
        .route(
            "/{bucket}/{*key}",
            put(put_object)
                .get(get_object)
                .head(head_object)
                .delete(delete_object)
                .post(post_object),
        )
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            auth::object_auth_middleware,
        ));

    Router::new()
        .route("/healthz", get(healthz))
        .route("/presign", post(presign))
        .route("/{bucket}", put(put_bucket).get(list_objects))
        .merge(object_routes)
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

#[derive(Debug, Deserialize)]
struct PresignBody {
    /// HTTP method to authorize (`GET`, `PUT`, `DELETE`, `HEAD`, `POST`).
    method: String,
    bucket: String,
    key: String,
    /// Seconds from now until the URL expires (must be > 0).
    expires_in_secs: u64,
}

#[derive(Debug, Serialize)]
struct PresignResponse {
    /// Path + query to use as the request URI (no host).
    url: String,
    expires: i64,
}

/// `POST /presign` — mint a presigned object URL.
///
/// Requires `Authorization: Bearer <ACCESS_KEY_ID>:<SECRET>` (or Bearer secret).
/// Returns 403 when auth is not configured or the bearer is wrong.
async fn presign(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<PresignBody>,
) -> Result<Json<PresignResponse>, AppError> {
    let auth = state.auth.as_ref().ok_or(AppError::AccessDenied)?;
    let authorization = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());
    if !auth::access_credentials_match(auth, authorization) {
        return Err(AppError::AccessDenied);
    }

    let bucket = Bucket::new(body.bucket)?;
    let key = Key::new(body.key)?;
    if body.expires_in_secs == 0 {
        return Err(AppError::InvalidRequest(
            "expires_in_secs must be greater than zero".into(),
        ));
    }

    let method = parse_presign_method(&body.method)?;
    let expires_at = Utc::now() + Duration::seconds(body.expires_in_secs as i64);
    let signed = auth::sign(
        auth,
        &PresignRequest {
            method,
            bucket: bucket.into_string(),
            key: key.into_string(),
            expires_at,
        },
    )?;

    Ok(Json(PresignResponse {
        url: signed.path_and_query,
        expires: expires_at.timestamp(),
    }))
}

fn parse_presign_method(raw: &str) -> Result<Method, AppError> {
    match raw.trim().to_ascii_uppercase().as_str() {
        "GET" => Ok(Method::GET),
        "PUT" => Ok(Method::PUT),
        "DELETE" => Ok(Method::DELETE),
        "HEAD" => Ok(Method::HEAD),
        "POST" => Ok(Method::POST),
        _ => Err(AppError::InvalidRequest(format!(
            "unsupported presign method: {raw}"
        ))),
    }
}

/// `PUT /{bucket}` — create a bucket (V3 `CreateBucket`), OR replace its
/// lifecycle policy when `?lifecycle` is present (`PutBucketLifecycleConfiguration`).
///
/// # Errors
///
/// Returns an [`AppError`] if the index rejects the bucket (e.g. an invalid
/// name, or a storage failure), or — on the `?lifecycle` path — if the body is
/// not a valid policy, the policy fails [`LifecyclePolicy::validate`], or the
/// bucket does not exist.
async fn put_bucket(
    State(state): State<AppState>,
    bucket: Bucket,
    Query(q): Query<BucketQuery>,
    body: Bytes,
) -> Result<StatusCode, AppError> {
    if q.lifecycle.is_some() {
        return put_bucket_lifecycle(&state, &bucket, &body).await;
    }
    state.index.create_bucket(&bucket).await?;
    Ok(StatusCode::OK)
}

/// Parse, validate, and durably store a bucket's lifecycle policy.
///
/// The bucket must already exist ([`Index::ensure_bucket`] errors
/// [`AppError::NoSuchBucket`] otherwise) — S3 rejects a lifecycle config on a
/// missing bucket. The policy is validated *before* it touches disk so an
/// incoherent rule (tier ≥ expire, zero age) never gets persisted.
async fn put_bucket_lifecycle(
    state: &AppState,
    bucket: &Bucket,
    body: &[u8],
) -> Result<StatusCode, AppError> {
    let policy: LifecyclePolicy = serde_json::from_slice(body)
        .map_err(|e| invalid_err(format!("invalid lifecycle policy: {e}")))?;
    policy.validate()?;

    state.index.ensure_bucket(bucket).await?;
    let mut meta = state.index.load_bucket_metadata(bucket).await?;
    meta.lifecycle = policy;
    state.index.store_bucket_metadata(bucket, &meta).await?;
    Ok(StatusCode::OK)
}

/// `GET /{bucket}?lifecycle` — return the bucket's lifecycle policy as JSON
/// (`GetBucketLifecycleConfiguration`). An unset policy is an empty rule list.
async fn get_bucket_lifecycle(state: &AppState, bucket: &Bucket) -> Result<Response, AppError> {
    let meta = state.index.load_bucket_metadata(bucket).await?;
    Ok(Json(meta.lifecycle).into_response())
}

/// `GET /{bucket}` — list objects (V3 `ListObjectsV2`).
///
/// Returns a S3 `<ListBucketResult>` XML body (`Content-Type: application/xml`).
async fn list_objects(
    State(state): State<AppState>,
    bucket: Bucket,
    Query(q): Query<ListQuery>,
) -> Result<Response, AppError> {
    if q.lifecycle.is_some() {
        return get_bucket_lifecycle(&state, &bucket).await;
    }
    let prefix = q.prefix.as_deref().unwrap_or("");
    let max_keys = q.max_keys.unwrap_or(1000);
    let listing = state
        .index
        .list(
            &bucket,
            prefix,
            q.delimiter.as_deref(),
            q.continuation_token.as_deref(),
            max_keys,
        )
        .await?;

    let contents: Vec<ListContent> = listing
        .objects
        .iter()
        .filter_map(|o| {
            let live = o.latest_live()?;
            Some(ListContent {
                key: o.key.to_string(),
                last_modified: live.last_modified,
                etag: live.etag.0,
                size: live.size,
            })
        })
        .collect();

    let body = list_bucket_result(&ListBucketParams {
        name: bucket.as_str(),
        prefix,
        delimiter: q.delimiter.as_deref(),
        max_keys,
        is_truncated: listing.next_continuation_token.is_some(),
        next_continuation_token: listing.next_continuation_token.as_deref(),
        contents: &contents,
        common_prefixes: &listing.common_prefixes,
    });
    Ok(xml_response(StatusCode::OK, body))
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
/// Auth is enforced by [`auth::object_auth_middleware`] on the object router.
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
    ObjectPath { bucket, key }: ObjectPath,
    Query(q): Query<PutQuery>,
    headers: HeaderMap,
    ChecksumSpec(checksum_algo): ChecksumSpec,
    body: Body,
) -> Result<Response, AppError> {
    let span = make_span(bucket.as_str(), key.as_str());

    let _guard = crate::metrics::InFlightGuard::new();
    let content_type = content_type_of(&headers);
    let stream = body.into_data_stream();

    let pre = parse_write_precondition(&headers)?;

    if let (Some(upload_id), Some(part_number)) = (q.upload_id.as_deref(), q.part_number) {
        let part = state
            .multipart
            .upload_part(upload_id, part_number, stream, state.max_object_size)
            .await?;
        return Ok((StatusCode::OK, [(header::ETAG, part.etag.0)]).into_response());
    }

    let streaming::Stored { digest, etag, size } =
        streaming::stream_to_store(&state.store, stream, state.max_object_size, checksum_algo)
            .await?;

    state
        .index
        .put(
            &bucket,
            &key,
            crate::index::NewVersion {
                digest,
                etag: etag.clone(),
                size,
                content_type,
            },
            pre,
        )
        .await?;
    span.record(SIZE_KEY, size);
    Ok((StatusCode::OK, [(header::ETAG, etag.0)]).into_response())
}

/// Parse a write's conditional headers into a [`Precondition`] for `index.put`
/// to enforce atomically. Only the S3-meaningful cases on a PUT are honoured:
///
/// - `If-None-Match: *` → create-once ([`Precondition::IfNoneMatchStar`]).
/// - `If-Match: <etag>` → compare-and-swap ([`Precondition::IfMatch`]).
/// - neither → [`Precondition::None`].
///
/// `If-None-Match: *` wins if both are present. A non-`*` `If-None-Match` on a
/// PUT isn't a compatibility case we support, so it's ignored (not an error).
/// A header that isn't valid ASCII is a `400`, never a panic.
fn parse_write_precondition(headers: &HeaderMap) -> Result<Precondition, AppError> {
    if let Some(inm) = headers.get(header::IF_NONE_MATCH) {
        let inm = inm
            .to_str()
            .map_err(|_| AppError::InvalidRequest("invalid If-None-Match header".into()))?;
        if inm.trim() == "*" {
            return Ok(Precondition::IfNoneMatchStar);
        }
    }
    if let Some(im) = headers.get(header::IF_MATCH) {
        let im = im
            .to_str()
            .map_err(|_| AppError::InvalidRequest("invalid If-Match header".into()))?;
        return Ok(Precondition::IfMatch(ETag(im.to_string())));
    }
    Ok(Precondition::None)
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
    ObjectPath { bucket, key }: ObjectPath,
    Query(q): Query<GetQuery>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(bucket.as_str(), key.as_str());
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

    if start > end {
        return Err(invalid_err(format!(
            "invalid range: start={start} end={end}"
        )));
    }
    let response_size = end - start + 1;

    let body = match state.lifecycle.locate(&meta.digest).await?.encoding {
        crate::lifecycle::Encoding::Raw => {
            let file = state
                .store
                .open_blob_range(&meta.digest, start, end)
                .await?;
            let reader = crate::metrics::ObservedDownload::new(file);
            Body::from_stream(tokio_util::io::ReaderStream::new(reader))
        }
        crate::lifecycle::Encoding::Zstd => {
            use tokio::io::AsyncReadExt;
            if end >= meta.size {
                return Err(invalid_err(format!(
                    "invalid range: start={start} end={end} size={}",
                    meta.size
                )));
            }
            let mut reader = state.lifecycle.open_tiered(&meta.digest).await?;
            if start > 0 {
                let mut skip = (&mut reader).take(start);
                tokio::io::copy(&mut skip, &mut tokio::io::sink()).await?;
            }
            let reader = crate::metrics::ObservedDownload::new(reader.take(response_size));
            Body::from_stream(tokio_util::io::ReaderStream::new(reader))
        }
    };

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
    ObjectPath { bucket, key }: ObjectPath,
    Query(q): Query<GetQuery>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let span = make_span(bucket.as_str(), key.as_str());
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
#[derive(Debug, Deserialize, Default)]
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
    ObjectPath { bucket, key }: ObjectPath,
    Query(q): Query<DeleteQuery>,
) -> Result<StatusCode, AppError> {
    let span = make_span(bucket.as_str(), key.as_str());
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
    ObjectPath { bucket, key }: ObjectPath,
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
        let body = initiate_multipart_result(bucket.as_str(), key.as_str(), &upload_id);
        return Ok(xml_response(StatusCode::OK, body));
    }

    // CompleteMultipartUpload: POST .../{key}?uploadId=…  (body lists the parts)
    if let Some(upload_id) = q.upload_id.as_deref() {
        let bytes = axum::body::to_bytes(body, usize::MAX)
            .await
            .map_err(|e| AppError::InvalidRequest(format!("failed to read body: {e}")))?;
        let ct = headers
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok());
        let parts = parse_complete_multipart_body(ct, &bytes)?;
        let meta = state.multipart.complete(upload_id, parts).await?;
        let live = meta.latest_live().ok_or(AppError::NoSuchKey)?;
        let body =
            complete_multipart_result(meta.bucket.as_str(), meta.key.as_str(), live.etag.as_str());
        return Ok(xml_response(StatusCode::OK, body));
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
    index: &IndexBackend,
    bucket: &Bucket,
    key: &Key,
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

/// Query params on `PUT /{bucket}` — distinguishes `CreateBucket` from
/// `PutBucketLifecycleConfiguration` by the valueless `?lifecycle` flag.
#[derive(Debug, Deserialize)]
struct BucketQuery {
    lifecycle: Option<String>,
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
    /// S3 clients send `list-type=2` for ListObjectsV2; we accept and ignore it.
    #[serde(rename = "list-type")]
    _list_type: Option<String>,
    lifecycle: Option<String>,
}

/// Query params on `GET` / `HEAD /{bucket}/{key}` — optional `versionId` pins a
/// historical version; absent means latest.
#[derive(Debug, Deserialize, Default)]
struct GetQuery {
    #[serde(rename = "versionId")]
    version_id: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::AuthConfig;
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
        put_bytes_if_match(router, bucket, key, body, None).await.0
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
    async fn list_xml_includes_live_object_key() {
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
        // ListObjectsV2 XML has no versionId; latest is whatever GET without
        // ?versionId= returns (see get_with_version_id_serves_that_version).
        let listing = crate::s3_xml::parse_list_bucket(&body).expect("list XML");
        assert_eq!(listing.object_keys(), vec!["readme".to_string()]);
        assert_eq!(listing.contents[0].size, 2); // live version is "v2"
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
        let err = crate::s3_xml::parse_error(&body).expect("error XML");
        assert_eq!(err.code, "InvalidRequest");
        assert!(
            err.message.contains("unrecognised POST"),
            "bare POST must explain the missing multipart query, got {:?}",
            err.message
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

        let (status, _, _) = put_bytes_if_match(&router, "docs", "k", b"v2", Some(&etag)).await;
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
        let err = crate::s3_xml::parse_error(&body).expect("error XML");
        assert_eq!(err.code, "PreconditionFailed");
        assert_eq!(err.message, "precondition failed");

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

    fn auth_router() -> (TempDir, Router, AuthConfig) {
        let dir = TempDir::new().expect("temp data dir");
        let auth = AuthConfig::new("route-akid", "route-test-secret");
        let state = AppState::open(dir.path(), 1 << 20)
            .expect("open AppState")
            .with_auth(Some(auth.clone()));
        (dir, router(state), auth)
    }

    #[tokio::test]
    async fn unsigned_put_is_forbidden_when_auth_enabled() {
        let (_dir, router, _auth) = auth_router();
        ensure_bucket(&router, "docs").await;
        let status = put_bytes(&router, "docs", "a.txt", b"nope").await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn presign_then_put_get_round_trips() {
        let (_dir, router, auth) = auth_router();
        ensure_bucket(&router, "docs").await;

        let (status, _, body) = send(
            &router,
            Request::builder()
                .method("POST")
                .uri("/presign")
                .header(header::AUTHORIZATION, "Bearer route-akid:route-test-secret")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(
                    r#"{"method":"PUT","bucket":"docs","key":"a.txt","expires_in_secs":60}"#,
                ))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let minted: serde_json::Value = serde_json::from_slice(&body).expect("json");
        let put_url = minted["url"].as_str().expect("url");

        let (status, _, _) = send(
            &router,
            Request::builder()
                .method("PUT")
                .uri(put_url)
                .header(header::CONTENT_TYPE, "text/plain")
                .body(Body::from(b"hello".to_vec()))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);

        let get_signed = auth::sign(
            &auth,
            &PresignRequest {
                method: Method::GET,
                bucket: "docs".into(),
                key: "a.txt".into(),
                expires_at: Utc::now() + Duration::seconds(60),
            },
        )
        .unwrap();
        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri(get_signed.path_and_query)
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"hello");
    }

    #[tokio::test]
    async fn presign_without_bearer_is_forbidden() {
        let (_dir, router, _auth) = auth_router();
        let (status, _, _) = send(
            &router,
            Request::builder()
                .method("POST")
                .uri("/presign")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(
                    r#"{"method":"GET","bucket":"docs","key":"a.txt","expires_in_secs":60}"#,
                ))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn healthz_stays_open_when_auth_enabled() {
        let (_dir, router, _auth) = auth_router();
        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"ok");
    }

    #[tokio::test]
    async fn put_with_access_credentials_works_without_presign() {
        let (_dir, router, auth) = auth_router();
        ensure_bucket(&router, "docs").await;

        let (status, _, _) = send(
            &router,
            Request::builder()
                .method("PUT")
                .uri("/docs/cred.txt")
                .header(
                    header::AUTHORIZATION,
                    format!("Bearer {}", auth.credential_token()),
                )
                .header(header::CONTENT_TYPE, "text/plain")
                .body(Body::from(b"via-creds".to_vec()))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);

        let (status, _, body) = send(
            &router,
            Request::builder()
                .uri("/docs/cred.txt")
                .header(
                    header::AUTHORIZATION,
                    format!("Bearer {}", auth.credential_token()),
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(&body[..], b"via-creds");
    }
}
