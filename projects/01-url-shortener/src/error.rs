//! A single application error type that knows how to turn itself into an HTTP
//! response. Handlers return `Result<T, AppError>` and use `?` freely.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("not found")]
    NotFound,

    #[error("unauthorized")]
    Unauthorized,

    #[error("bad request: {0}")]
    BadRequest(String),

    #[error("too many requests")]
    RateLimited,

    #[error(transparent)]
    Database(#[from] sqlx::Error),

    #[error(transparent)]
    Cache(#[from] redis::RedisError),

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::RateLimited => StatusCode::TOO_MANY_REQUESTS,
            Self::Database(_) | Self::Cache(_) | Self::Other(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }

        let client_msg = if status.is_server_error() {
            "internal server error".to_string()
        } else {
            self.to_string()
        };

        (status, Json(json!({ "error": client_msg }))).into_response()
    }
}
