//! Security — per-key rate limiting for the write path (`POST /api/links`).
//!
//! Project 01 only needs a *taste* of abuse protection: the from-scratch
//! token-bucket + sliding-window + Redis-Lua build is the whole vertical of
//! project 02 (the distributed rate limiter). So here we lean on
//! [`tower_governor`] (GCRA, a token-bucket variant) and supply a custom
//! [`KeyExtractor`] that buckets on the **API key** instead of the client IP.
//!
//! The layer itself is built and wired in `routes.rs` (where the middleware
//! generic is inferred) as a `route_layer` on the protected group, *after*
//! `require_api_key` so every request the limiter sees already carries a
//! validated bearer token. Scoped to `POST`, so the `GET` stats endpoint and
//! the public redirect are never throttled here.

use axum::http::{header, Request};
use tower_governor::errors::GovernorError;
use tower_governor::key_extractor::KeyExtractor;

/// Replenish one request to a key's bucket every this many milliseconds
/// (200ms ⇒ ~5 sustained requests/sec once the burst budget is spent).
pub const RATE_LIMIT_PERIOD_MS: u64 = 200;
/// Largest burst a single API key may spend before it must wait for refills.
pub const RATE_LIMIT_BURST: u32 = 10;

/// A [`KeyExtractor`] that rate-limits per **API key** (the `Authorization:
/// Bearer <token>` value) rather than per IP — so one noisy client can't
/// exhaust the budget for everyone sharing a proxy/NAT.
///
/// By the time this runs, `require_api_key` has already validated the token, so
/// a missing/malformed header here would mean the layers are mis-ordered; we
/// surface that as [`GovernorError::UnableToExtractKey`] (HTTP 500) rather than
/// silently bucketing every caller under one empty key.
///
/// Only `extract` is implemented: the trait's `name`/`key_name` hooks exist
/// solely under `tower_governor`'s `tracing` feature, which we don't enable
/// (that also keeps the raw key out of any trace output).
#[derive(Clone, Copy, Debug)]
pub struct ApiKeyExtractor;

impl KeyExtractor for ApiKeyExtractor {
    type Key = String;

    fn extract<T>(&self, req: &Request<T>) -> Result<Self::Key, GovernorError> {
        req.headers()
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|auth| auth.strip_prefix("Bearer "))
            .map(|token| token.to_owned())
            .ok_or(GovernorError::UnableToExtractKey)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    use proptest::prelude::*;

    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use axum::routing::post;
    use axum::Router;
    use tower::ServiceExt;
    use tower_governor::governor::GovernorConfigBuilder;
    use tower_governor::GovernorLayer;

    fn request_with_auth(value: Option<&str>) -> Request<()> {
        let mut builder = Request::builder().uri("/");
        if let Some(value) = value {
            builder = builder.header(header::AUTHORIZATION, value);
        }
        builder.body(()).unwrap()
    }

    #[test]
    fn extract_returns_the_bearer_token() {
        let req = request_with_auth(Some("Bearer secret-key"));
        assert_eq!(ApiKeyExtractor.extract(&req).unwrap(), "secret-key");
    }

    #[test]
    fn extract_rejects_missing_header() {
        let req = request_with_auth(None);
        assert!(matches!(
            ApiKeyExtractor.extract(&req),
            Err(GovernorError::UnableToExtractKey)
        ));
    }

    #[test]
    fn extract_rejects_non_bearer_scheme() {
        let req = request_with_auth(Some("Basic secret-key"));
        assert!(matches!(
            ApiKeyExtractor.extract(&req),
            Err(GovernorError::UnableToExtractKey)
        ));
    }

    proptest! {
        /// Round-trip: `extract` inverts the `Bearer `-prefix wrapping for *any*
        /// token. The strategy is restricted to visible ASCII (` ` through `~`)
        /// so every value is a legal HTTP header value — otherwise
        /// `Request::builder` would reject it — and survives `HeaderValue::to_str`.
        /// The empty string is generated on purpose: `"Bearer "` strips to
        /// `Ok("")`, so an empty key round-trips too; `require_api_key` is what
        /// guarantees a non-empty token upstream. We compare via `.ok()` because
        /// `GovernorError` isn't `PartialEq`.
        #[test]
        fn prop_extract_inverts_bearer_prefix(token in "[ -~]*") {
            let req = request_with_auth(Some(&format!("Bearer {token}")));
            prop_assert_eq!(ApiKeyExtractor.extract(&req).ok(), Some(token));
        }

        /// Any header value lacking the exact `Bearer ` scheme prefix is rejected
        /// — wrong scheme, wrong case, or a missing trailing space all land here.
        /// `prop_assume!` discards the rare random values that happen to form a
        /// valid bearer header so they don't fail this negation.
        #[test]
        fn prop_rejects_without_bearer_prefix(value in "[ -~]*") {
            prop_assume!(!value.starts_with("Bearer "));
            let req = request_with_auth(Some(&value));
            prop_assert!(matches!(
                ApiKeyExtractor.extract(&req),
                Err(GovernorError::UnableToExtractKey)
            ));
        }
    }

    /// A throwaway router that rate-limits a dummy handler by API key with the
    /// given burst and ~no refill during a test (one cell per hour), so we can
    /// assert exactly when the limiter trips. Reuses the production
    /// [`ApiKeyExtractor`]; the keyed bucket store lives in the shared
    /// `GovernorConfig`, so `router.clone()` keeps hitting the same buckets.
    fn limited_router(burst: u32) -> Router {
        let config = Arc::new(
            GovernorConfigBuilder::default()
                .per_second(3600)
                .burst_size(burst)
                .key_extractor(ApiKeyExtractor)
                .finish()
                .unwrap(),
        );
        Router::new()
            .route("/", post(|| async { StatusCode::OK }))
            .route_layer(GovernorLayer { config })
    }

    async fn post_with_key(router: &Router, key: &str) -> StatusCode {
        let req = Request::builder()
            .method("POST")
            .uri("/")
            .header(header::AUTHORIZATION, format!("Bearer {key}"))
            .body(Body::empty())
            .unwrap();
        router.clone().oneshot(req).await.unwrap().status()
    }

    #[tokio::test]
    async fn allows_burst_then_limits_same_key() {
        let router = limited_router(2);
        assert_eq!(post_with_key(&router, "k1").await, StatusCode::OK);
        assert_eq!(post_with_key(&router, "k1").await, StatusCode::OK);
        assert_eq!(
            post_with_key(&router, "k1").await,
            StatusCode::TOO_MANY_REQUESTS
        );
    }

    #[tokio::test]
    async fn buckets_are_independent_per_key() {
        let router = limited_router(1);
        assert_eq!(post_with_key(&router, "k1").await, StatusCode::OK);
        assert_eq!(
            post_with_key(&router, "k1").await,
            StatusCode::TOO_MANY_REQUESTS
        );
        // A different key has its own budget and is unaffected.
        assert_eq!(post_with_key(&router, "k2").await, StatusCode::OK);
    }
}
