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
use axum::http::header::{CONTENT_LENGTH, CONTENT_TYPE, ETAG};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, put};
use axum::{Json, Router};
use chrono::Utc;
use serde::Deserialize;
use serde_json::json;
use tower_http::trace::TraceLayer;

use crate::error::AppError;
use crate::object::ObjectMeta;
use crate::{streaming, AppState};

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        // Bucket-level: create + list.
        .route("/{bucket}", put(create_bucket).get(list_objects))
        // Object-level: put/get/delete + the multipart verbs (query-dispatched).
        .route(
            "/{bucket}/{*key}",
            put(put_object)
                .get(get_object)
                .delete(delete_object)
                .post(post_object),
        )
        // Objects stream — disable axum's 2 MB default body limit, which would
        // truncate every real upload. The real cap (MAX_OBJECT_SIZE) is enforced
        // in the V2 stream loop instead.
        .layer(DefaultBodyLimit::disable())
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

/// `PUT /{bucket}` — create a bucket (V3).
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
async fn put_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<PutQuery>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, AppError> {
    let content_type = content_type_of(&headers);

    // UploadPart: a PUT carrying ?uploadId & ?partNumber (V4).
    if let (Some(upload_id), Some(part_number)) = (q.upload_id.as_deref(), q.part_number) {
        let part = state
            .multipart
            .upload_part(
                upload_id,
                part_number,
                body.into_data_stream(),
                state.max_object_size,
            )
            .await?;
        return Ok((StatusCode::OK, [(ETAG, part.etag.0)]).into_response());
    }

    // Plain single PUT: stream the body to the store (V2), then index it (V3).
    let stored =
        streaming::stream_to_store(&state.store, body.into_data_stream(), state.max_object_size)
            .await?;
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
    Ok((StatusCode::OK, [(ETAG, stored.etag.0)]).into_response())
}

/// `GET /{bucket}/{key}` — stream an object back (V2). Fully wired: it looks up
/// the metadata (V3), opens the blob (V1), and streams the file as the body so
/// the bytes never all sit in memory at once.
///
/// TODO(V4 / protocol): honour a `Range:` header → `206 Partial Content` with a
/// `Content-Range`, and `If-None-Match` on the ETag → `304 Not Modified`.
async fn get_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    let _ = &headers; // TODO(V4): Range / If-None-Match live here.

    let meta = state
        .index
        .get(&bucket, &key)
        .await?
        .ok_or(AppError::NoSuchKey)?;

    let file = state.store.open_blob(&meta.digest).await?;
    let body = Body::from_stream(tokio_util::io::ReaderStream::new(file));

    Ok((
        [
            (CONTENT_TYPE, meta.content_type),
            (ETAG, meta.etag.0),
            (CONTENT_LENGTH, meta.size.to_string()),
        ],
        body,
    )
        .into_response())
}

/// `DELETE /{bucket}/{key}` — delete a key (V3), OR abort a multipart upload
/// when `?uploadId` is present (V4).
async fn delete_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<DeleteQuery>,
) -> Result<StatusCode, AppError> {
    if let Some(upload_id) = q.upload_id.as_deref() {
        state.multipart.abort(upload_id).await?;
        return Ok(StatusCode::NO_CONTENT);
    }
    state.index.delete(&bucket, &key).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// `POST /{bucket}/{key}` — the multipart control verbs (V4), dispatched on the
/// query: `?uploads` initiates, `?uploadId=…` completes.
///
/// TODO(protocol): both the request body (CompleteMultipartUpload part list) and
/// the responses are XML in real S3; we accept/emit JSON placeholders for now.
async fn post_object(
    State(state): State<AppState>,
    Path((bucket, key)): Path<(String, String)>,
    Query(q): Query<PostQuery>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, AppError> {
    // InitiateMultipartUpload: POST .../{key}?uploads
    if q.uploads.is_some() {
        let content_type = content_type_of(&headers);
        let upload_id = state
            .multipart
            .initiate(&bucket, &key, content_type)
            .await?;
        return Ok(Json(json!({
            "bucket": bucket,
            "key": key,
            "uploadId": upload_id,
        }))
        .into_response());
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
            "bucket": meta.bucket,
            "key": meta.key,
            "etag": meta.etag.0,
        }))
        .into_response());
    }

    Err(AppError::InvalidRequest(
        "unrecognised POST (expected ?uploads or ?uploadId)".into(),
    ))
}

/// Read the `Content-Type` header, defaulting to the S3 default for objects.
fn content_type_of(headers: &HeaderMap) -> String {
    headers
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string()
}

#[derive(Debug, Deserialize)]
struct PutQuery {
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
    #[serde(rename = "partNumber")]
    part_number: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct PostQuery {
    /// Present (valueless `?uploads`) for InitiateMultipartUpload.
    uploads: Option<String>,
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DeleteQuery {
    #[serde(rename = "uploadId")]
    upload_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    prefix: Option<String>,
    delimiter: Option<String>,
    #[serde(rename = "continuation-token")]
    continuation_token: Option<String>,
    #[serde(rename = "max-keys")]
    max_keys: Option<usize>,
}
