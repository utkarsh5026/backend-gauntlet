//! HTTP surface for the index microservice binary (`object-store-index`).
//!
//! Internal JSON API (not S3 path-style). Errors are JSON
//! ([`IndexErrorBody`](crate::index_backend::IndexErrorBody)) so
//! [`RemoteIndex`](crate::index_backend::RemoteIndex) can map status + code
//! back to [`AppError`](crate::error::AppError).

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};

use crate::bucket::BucketMetadata;
use crate::error::AppError;
use crate::index::Index;
use crate::index_backend::{
    GcResponse, IndexErrorBody, ListQuery, ListingWire, ObjectRefBody, PutRequest,
    ResolvedObjectWire,
};
use crate::object::ObjectMeta;
use crate::s3_xml::error_code;

/// Shared state for the index service process.
#[derive(Clone)]
pub struct IndexServiceState {
    pub index: Arc<Index>,
}

/// Wrap [`AppError`] as a JSON response for the internal API.
struct IndexApiError(AppError);

impl From<AppError> for IndexApiError {
    fn from(e: AppError) -> Self {
        Self(e)
    }
}

impl IntoResponse for IndexApiError {
    fn into_response(self) -> Response {
        let status = match &self.0 {
            AppError::NoSuchBucket | AppError::NoSuchKey | AppError::NoSuchUpload => {
                StatusCode::NOT_FOUND
            }
            AppError::BucketAlreadyExists => StatusCode::CONFLICT,
            AppError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
            AppError::EntityTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::PreconditionFailed => StatusCode::PRECONDITION_FAILED,
            AppError::Io(_) | AppError::Other(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };

        if status.is_server_error() {
            tracing::error!(error = %self.0, "index service request failed");
        }

        let message = if status.is_server_error() {
            "internal server error".to_string()
        } else {
            self.0.to_string()
        };

        (
            status,
            Json(IndexErrorBody {
                code: error_code(&self.0).to_string(),
                message,
            }),
        )
            .into_response()
    }
}

/// Internal metadata API — not the S3 path-style surface.
pub fn router(state: IndexServiceState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/buckets", get(list_buckets))
        .route(
            "/v1/buckets/{bucket}",
            put(create_bucket).head(ensure_bucket),
        )
        .route(
            "/v1/buckets/{bucket}/metadata",
            get(get_bucket_metadata).put(put_bucket_metadata),
        )
        // Resolve uses its own prefix — axum forbids nesting under `{*key}`.
        .route("/v1/buckets/{bucket}/resolve/{*key}", post(resolve_key))
        .route(
            "/v1/buckets/{bucket}/keys/{*key}",
            put(put_key).get(get_key).delete(delete_key),
        )
        .route("/v1/buckets/{bucket}/list", get(list_objects))
        .route("/v1/buckets/{bucket}/entries", get(index_entries))
        .route("/v1/gc", post(gc))
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok"
}

async fn list_buckets(
    State(state): State<IndexServiceState>,
) -> Result<Json<Vec<String>>, IndexApiError> {
    Ok(Json(state.index.buckets().await?))
}

async fn create_bucket(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
) -> Result<StatusCode, IndexApiError> {
    state.index.create_bucket(&bucket).await?;
    Ok(StatusCode::OK)
}

async fn ensure_bucket(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
) -> Result<StatusCode, IndexApiError> {
    state.index.ensure_bucket(&bucket).await?;
    Ok(StatusCode::OK)
}

async fn get_bucket_metadata(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
) -> Result<Json<BucketMetadata>, IndexApiError> {
    let dir = state.index.ensure_bucket(&bucket).await?;
    Ok(Json(BucketMetadata::load(&dir).await?))
}

async fn put_bucket_metadata(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
    Json(meta): Json<BucketMetadata>,
) -> Result<StatusCode, IndexApiError> {
    let dir = state.index.ensure_bucket(&bucket).await?;
    meta.store(&dir).await?;
    Ok(StatusCode::OK)
}

async fn put_key(
    State(state): State<IndexServiceState>,
    Path((bucket, key)): Path<(String, String)>,
    Json(body): Json<PutRequest>,
) -> Result<Json<ObjectMeta>, IndexApiError> {
    let (version, pre) = body.into_parts();
    Ok(Json(state.index.put(&bucket, &key, version, pre).await?))
}

async fn get_key(
    State(state): State<IndexServiceState>,
    Path((bucket, key)): Path<(String, String)>,
) -> Result<Json<Option<ObjectMeta>>, IndexApiError> {
    Ok(Json(state.index.get(&bucket, &key).await?))
}

async fn delete_key(
    State(state): State<IndexServiceState>,
    Path((bucket, key)): Path<(String, String)>,
    Json(body): Json<ObjectRefBody>,
) -> Result<StatusCode, IndexApiError> {
    state
        .index
        .delete(&bucket, &key, body.object_ref.into())
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn resolve_key(
    State(state): State<IndexServiceState>,
    Path((bucket, key)): Path<(String, String)>,
    Json(body): Json<ObjectRefBody>,
) -> Result<Json<ResolvedObjectWire>, IndexApiError> {
    let resolved = state
        .index
        .resolve(&bucket, &key, body.object_ref.into())
        .await?;
    Ok(Json(resolved.into()))
}

async fn list_objects(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
    Query(q): Query<ListQuery>,
) -> Result<Json<ListingWire>, IndexApiError> {
    let listing = state
        .index
        .list(
            &bucket,
            &q.prefix,
            q.delimiter.as_deref(),
            q.continuation.as_deref(),
            q.max_keys,
        )
        .await?;
    Ok(Json(listing.into()))
}

async fn index_entries(
    State(state): State<IndexServiceState>,
    Path(bucket): Path<String>,
) -> Result<Json<Vec<ObjectMeta>>, IndexApiError> {
    Ok(Json(state.index.index_entries(&bucket).await?))
}

async fn gc(State(state): State<IndexServiceState>) -> Result<Json<GcResponse>, IndexApiError> {
    let reclaimed = state.index.gc().await?;
    Ok(Json(GcResponse { reclaimed }))
}
