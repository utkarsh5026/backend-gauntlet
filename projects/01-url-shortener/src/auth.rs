//! Security — API-key auth middleware for the write/stats endpoints.
//!
//! Applied as an axum middleware layer in `routes.rs`. Public redirects skip it.

use axum::extract::State;
use axum::http::{header, Request};
use axum::middleware::Next;
use axum::response::Response;

use crate::error::AppError;
use crate::AppState;

/// Reject requests that don't present a valid API key.
///
/// Expected header: `Authorization: Bearer <key>`.
///
/// TODO(security):
/// - Parse the bearer token from the `Authorization` header.
/// - Check it against `state.api_keys` using a **constant-time** comparison
///   (avoid leaking validity via timing — see the `subtle` crate or hash both
///   sides and compare). A plain `HashSet::contains` is the easy version; note
///   in docs/01-design.md why constant-time matters and what you chose.
/// - Never log the key itself.
/// - In a real system keys would be hashed at rest in the DB, not held in memory.
pub async fn require_api_key(
    State(state): State<AppState>,
    req: Request<axum::body::Body>,
    next: Next,
) -> Result<Response, AppError> {
    let authorized = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|authorization| authorization.strip_prefix("Bearer "))
        .is_some_and(|token| state.api_keys.contains(token));

    if authorized {
        return Ok(next.run(req).await);
    }

    Err(AppError::Unauthorized)
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use axum::body::Body;
    use axum::http::{header, HeaderValue, Request, StatusCode};
    use axum::middleware::from_fn_with_state;
    use axum::routing::get;
    use axum::Router;
    use redis::Client;
    use sqlx::postgres::PgPoolOptions;
    use tokio::sync::mpsc;
    use tower::ServiceExt;

    use crate::cache::Cache;
    use crate::id_gen::IdGenerator;
    use crate::AppState;

    async fn test_state(api_keys: &[&str]) -> AppState {
        let db = PgPoolOptions::new()
            .connect_lazy("postgres://localhost:5432/unused")
            .expect("lazy pool");
        let redis = Client::open("redis://127.0.0.1:6379/").expect("redis client");
        let conn = redis::aio::ConnectionManager::new(redis)
            .await
            .expect("redis connection manager");
        let (clicks_tx, _clicks_rx) = mpsc::channel(1);

        AppState {
            db,
            cache: Cache::new(conn),
            ids: Arc::new(IdGenerator::new(0)),
            clicks: clicks_tx,
            api_keys: Arc::new(api_keys.iter().map(|s| (*s).to_string()).collect()),
            base_url: Arc::from("http://localhost:8080"),
        }
    }

    fn auth_router(state: AppState) -> Router {
        Router::new()
            .route("/", get(|| async { StatusCode::OK }))
            .route_layer(from_fn_with_state(state, super::require_api_key))
    }

    async fn status_for_authorization(state: AppState, authorization: Option<&str>) -> StatusCode {
        let mut builder = Request::builder().uri("/");
        if let Some(value) = authorization {
            builder = builder.header(header::AUTHORIZATION, value);
        }

        auth_router(state)
            .oneshot(builder.body(Body::empty()).unwrap())
            .await
            .expect("router response")
            .status()
    }

    async fn status_for_authorization_bytes(state: AppState, value: HeaderValue) -> StatusCode {
        auth_router(state)
            .oneshot(
                Request::builder()
                    .uri("/")
                    .header(header::AUTHORIZATION, value)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .expect("router response")
            .status()
    }

    #[tokio::test]
    async fn require_api_key_accepts_valid_bearer_token() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("Bearer dev-secret-key")).await,
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_unknown_key() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("Bearer wrong-key")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_missing_header() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, None).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_missing_bearer_prefix() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("dev-secret-key")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_wrong_prefix_casing() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("bearer dev-secret-key")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_accepts_token_with_spaces() {
        let state = test_state(&["key with spaces"]).await;
        assert_eq!(
            status_for_authorization(state, Some("Bearer key with spaces")).await,
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_trailing_space_on_token() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("Bearer dev-secret-key ")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_leading_space_on_token() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization(state, Some("Bearer  dev-secret-key")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn require_api_key_rejects_non_utf8_authorization() {
        let state = test_state(&["dev-secret-key"]).await;
        assert_eq!(
            status_for_authorization_bytes(state, HeaderValue::from_bytes(&[0xff, 0xfe]).unwrap())
                .await,
            StatusCode::UNAUTHORIZED
        );
    }
}
