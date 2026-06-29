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
/// Expected header: `Authorization: Bearer <key>`. On success the request is
/// forwarded to `next`; otherwise the chain is short-circuited before the
/// handler runs.
///
/// # Errors
///
/// Returns [`AppError::Unauthorized`] when the `Authorization` header is
/// missing, not valid UTF-8, lacks the `Bearer ` prefix, or carries a token
/// that isn't in `state.api_keys`.
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
    use axum::body::Body;
    use axum::http::{header, HeaderValue, Request, StatusCode};
    use axum::middleware::from_fn_with_state;
    use axum::routing::get;
    use axum::Router;
    use tower::ServiceExt;

    use crate::test_support::{app_state_with_db, lazy_pg_pool, test_cache};
    use crate::AppState;

    async fn test_state(api_keys: &[&str]) -> AppState {
        app_state_with_db(test_cache().await, api_keys, lazy_pg_pool()).await
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
