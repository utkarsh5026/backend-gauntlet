//! Dual-path object auth: **presigned URLs** or **access credentials**.
//!
//! When [`AuthConfig`] is installed on [`crate::AppState`], object routes are
//! gated by [`object_auth_middleware`] (wired via `route_layer` in
//! [`crate::routes`]). A request is accepted if **either**:
//!
//! 1. **Presigned URL** — query carries `expires` + `signature` (HMAC over
//!    method + bucket + key + expiry). Self-describing like a JWT: the server
//!    reconstructs the claims from the request and checks the MAC. Mint via
//!    `POST /presign`.
//! 2. **Access credentials** — `Authorization: Bearer <ACCESS_KEY_ID>:<SECRET>`
//!    (or `Bearer <SECRET>` alone). Lets the normal `PUT /{bucket}/{key}` path
//!    work without query params — the client proves it holds the long-lived key.
//!
//! `/healthz` and bucket create/list stay outside the object middleware. Mint
//! requires the same Bearer credentials. Never log secrets or write-granting
//! signed URLs.
//!
//! Simplified learning shape (not full AWS SigV4). Session-scoped tokens
//! (Express One Zone) can layer on later using the same HMAC brain.

use axum::body::Body;
use axum::extract::State;
use axum::http::header;
use axum::http::{Method, Request};
use axum::middleware::Next;
use axum::response::Response;
use chrono::{DateTime, Utc};
use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::error::AppError;

type HmacSha256 = Hmac<Sha256>;

/// Query parameter carrying the unix-seconds expiry on a presigned URL.
pub const QUERY_EXPIRES: &str = "expires";

/// Query parameter carrying the hex-encoded HMAC signature.
pub const QUERY_SIGNATURE: &str = "signature";

/// Default [`AuthConfig::access_key_id`] when `ACCESS_KEY_ID` is unset.
pub const DEFAULT_ACCESS_KEY_ID: &str = "local";

/// Long-lived credentials loaded from config / env.
///
/// Never log [`Self::secret_access_key`].
#[derive(Clone)]
pub struct AuthConfig {
    /// Public-ish key id (like AWS Access Key ID). Sent with the secret on
    /// credential-authenticated requests.
    pub access_key_id: String,
    pub secret_access_key: String,
}

impl AuthConfig {
    pub fn new(access_key_id: impl Into<String>, secret_access_key: impl Into<String>) -> Self {
        Self {
            access_key_id: access_key_id.into(),
            secret_access_key: secret_access_key.into(),
        }
    }

    /// Read `SECRET_ACCESS_KEY` (required) and optional `ACCESS_KEY_ID`.
    ///
    /// # Errors
    ///
    /// [`AppError::InvalidRequest`] when the secret is missing or empty.
    pub fn from_env() -> Result<Self, AppError> {
        let secret = std::env::var("SECRET_ACCESS_KEY")
            .map_err(|_| AppError::InvalidRequest("SECRET_ACCESS_KEY is not set".into()))?;
        if secret.is_empty() {
            return Err(AppError::InvalidRequest(
                "SECRET_ACCESS_KEY must not be empty".into(),
            ));
        }
        let access_key_id = std::env::var("ACCESS_KEY_ID")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| DEFAULT_ACCESS_KEY_ID.to_string());
        Ok(Self::new(access_key_id, secret))
    }

    /// Like [`from_env`], but `None` when the secret is unset/empty.
    pub fn from_env_optional() -> Option<Self> {
        Self::from_env().ok()
    }

    /// `ACCESS_KEY_ID:SECRET` — the preferred Bearer token for credential auth.
    pub fn credential_token(&self) -> String {
        format!("{}:{}", self.access_key_id, self.secret_access_key)
    }
}

/// What the client wants a presigned URL to authorize — the bytes that get signed.
#[derive(Debug, Clone)]
pub struct PresignRequest {
    pub method: Method,
    pub bucket: String,
    pub key: String,
    pub expires_at: DateTime<Utc>,
}

/// Signature material pulled off an incoming request's query string.
#[derive(Debug, Clone)]
pub struct PresignParams {
    /// Unix timestamp seconds (same instant as [`PresignRequest::expires_at`]).
    pub expires: i64,
    /// Hex-encoded MAC. Compare with a constant-time equality check.
    pub signature: String,
}

/// A path + query the client can hit until `expires`.
///
/// Example shape (simplified, not AWS wire format):
/// `/{bucket}/{key}?expires=…&signature=…`
#[derive(Debug, Clone)]
pub struct PresignedUrl {
    pub path_and_query: String,
}

/// Canonical string both [`sign`] and [`verify`] HMAC:
/// `{METHOD}\n/{bucket}/{key}\n{expires_unix}`
fn canonical_string(method: &Method, bucket: &str, key: &str, expires_unix: i64) -> String {
    format!("{method}\n/{bucket}/{key}\n{expires_unix}")
}

fn hmac_hex(secret: &str, canonical: &str) -> Result<String, AppError> {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .map_err(|_| AppError::Other(anyhow::anyhow!("hmac key rejected")))?;
    mac.update(canonical.as_bytes());
    Ok(hex::encode(mac.finalize().into_bytes()))
}

/// Length-checked constant-time compare so a caller cannot recover the
/// secret/signature byte-by-byte from timing. Early length mismatch leaks only length.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Mint a presigned URL for `req` using `config`'s secret.
///
/// # Errors
///
/// [`AppError::InvalidRequest`] when `expires_at` is not strictly in the future.
pub fn sign(config: &AuthConfig, req: &PresignRequest) -> Result<PresignedUrl, AppError> {
    let expires = req.expires_at.timestamp();
    if expires <= Utc::now().timestamp() {
        return Err(AppError::InvalidRequest(
            "presign expiry must be in the future".into(),
        ));
    }
    let canonical = canonical_string(&req.method, &req.bucket, &req.key, expires);
    let signature = hmac_hex(&config.secret_access_key, &canonical)?;
    Ok(PresignedUrl {
        path_and_query: format!(
            "/{}/{}?{}={}&{}={}",
            req.bucket, req.key, QUERY_EXPIRES, expires, QUERY_SIGNATURE, signature
        ),
    })
}

/// Accept or reject a presigned object request.
///
/// # Errors
///
/// [`AppError::AccessDenied`] when expired or the MAC does not match.
pub fn verify(
    config: &AuthConfig,
    method: &Method,
    bucket: &str,
    key: &str,
    params: &PresignParams,
    now: DateTime<Utc>,
) -> Result<(), AppError> {
    if now.timestamp() >= params.expires {
        return Err(AppError::AccessDenied);
    }
    let canonical = canonical_string(method, bucket, key, params.expires);
    let expected = hmac_hex(&config.secret_access_key, &canonical)?;
    if !constant_time_eq(expected.as_bytes(), params.signature.as_bytes()) {
        return Err(AppError::AccessDenied);
    }
    Ok(())
}

/// Parse [`PresignParams`] from a raw query string (`expires=…&signature=…`).
///
/// # Errors
///
/// [`AppError::AccessDenied`] when required params are missing or malformed.
pub fn parse_presign_params(query: Option<&str>) -> Result<PresignParams, AppError> {
    let query = query.ok_or(AppError::AccessDenied)?;
    let mut expires: Option<i64> = None;
    let mut signature: Option<String> = None;
    for pair in query.split('&') {
        let mut parts = pair.splitn(2, '=');
        let key = parts.next().unwrap_or("");
        let value = parts.next().unwrap_or("");
        match key {
            QUERY_EXPIRES => {
                expires = Some(value.parse().map_err(|_| AppError::AccessDenied)?);
            }
            QUERY_SIGNATURE => {
                if value.is_empty() {
                    return Err(AppError::AccessDenied);
                }
                signature = Some(value.to_string());
            }
            _ => {}
        }
    }
    match (expires, signature) {
        (Some(expires), Some(signature)) => Ok(PresignParams { expires, signature }),
        _ => Err(AppError::AccessDenied),
    }
}

/// Build [`PresignParams`] from optional query fields already extracted by axum.
///
/// # Errors
///
/// [`AppError::AccessDenied`] when either field is missing or empty.
pub fn params_from_parts(
    expires: Option<i64>,
    signature: Option<&str>,
) -> Result<PresignParams, AppError> {
    match (expires, signature) {
        (Some(expires), Some(signature)) if !signature.is_empty() => Ok(PresignParams {
            expires,
            signature: signature.to_string(),
        }),
        _ => Err(AppError::AccessDenied),
    }
}

/// True if `Authorization` presents valid long-lived credentials.
///
/// Accepted forms:
/// - `Bearer <ACCESS_KEY_ID>:<SECRET_ACCESS_KEY>` (preferred)
/// - `Bearer <SECRET_ACCESS_KEY>` (simple API-key style)
pub fn access_credentials_match(config: &AuthConfig, authorization: Option<&str>) -> bool {
    let Some(token) = authorization.and_then(|h| h.strip_prefix("Bearer ")) else {
        return false;
    };
    if let Some((id, secret)) = token.split_once(':') {
        return constant_time_eq(id.as_bytes(), config.access_key_id.as_bytes())
            && constant_time_eq(secret.as_bytes(), config.secret_access_key.as_bytes());
    }
    constant_time_eq(token.as_bytes(), config.secret_access_key.as_bytes())
}

/// Inputs for [`authorize_object`] — everything about one object request except
/// the long-lived [`AuthConfig`].
#[derive(Debug, Clone)]
pub struct ObjectAuthRequest<'a> {
    pub method: &'a Method,
    pub bucket: &'a str,
    pub key: &'a str,
    pub expires: Option<i64>,
    pub signature: Option<&'a str>,
    pub authorization: Option<&'a str>,
    pub now: DateTime<Utc>,
}

/// Authorize an object request: **presign if query present, else access credentials**.
///
/// If either `expires` or `signature` is present, the request is treated as
/// presigned (partial query → deny, no fall-through to Bearer). Otherwise the
/// `Authorization` header must carry valid access credentials.
///
/// # Errors
///
/// [`AppError::AccessDenied`] when neither path succeeds.
pub fn authorize_object(config: &AuthConfig, req: &ObjectAuthRequest<'_>) -> Result<(), AppError> {
    if req.expires.is_some() || req.signature.is_some() {
        let params = params_from_parts(req.expires, req.signature)?;
        return verify(config, req.method, req.bucket, req.key, &params, req.now);
    }
    if access_credentials_match(config, req.authorization) {
        return Ok(());
    }
    Err(AppError::AccessDenied)
}

/// Axum middleware: gate `/{bucket}/{*key}` before the handler runs.
///
/// Applied with `route_layer` on the object router only — `/healthz`, `/presign`,
/// and `/{bucket}` are unaffected. When [`crate::AppState::auth`] is `None`,
/// this is a no-op.
pub async fn object_auth_middleware(
    State(state): State<crate::AppState>,
    req: Request<Body>,
    next: Next,
) -> Result<Response, AppError> {
    let Some(auth) = state.auth.as_ref() else {
        return Ok(next.run(req).await);
    };

    let method = req.method().clone();

    let (bucket, key) = {
        let path = req.uri().path().trim_start_matches('/');
        let (bucket, key) = path.split_once('/').ok_or(AppError::AccessDenied)?;
        if bucket.is_empty() || key.is_empty() {
            return Err(AppError::AccessDenied);
        }
        (bucket, key)
    };

    let (expires, signature) = {
        let mut expires = None;
        let mut signature = None;
        if let Some(query) = req.uri().query() {
            for (key, value) in url::form_urlencoded::parse(query.as_bytes()) {
                match key.as_ref() {
                    QUERY_EXPIRES => {
                        expires = value.parse().ok();
                    }
                    QUERY_SIGNATURE if !value.is_empty() => {
                        signature = Some(value.into_owned());
                    }
                    _ => {}
                }
            }
        }
        (expires, signature)
    };

    let authorization = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);

    authorize_object(
        auth,
        &ObjectAuthRequest {
            method: &method,
            bucket,
            key,
            expires,
            signature: signature.as_deref(),
            authorization: authorization.as_deref(),
            now: Utc::now(),
        },
    )?;

    Ok(next.run(req).await)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn cfg() -> AuthConfig {
        AuthConfig::new("AKIATEST", "test-secret-do-not-use-in-prod")
    }

    fn future() -> DateTime<Utc> {
        Utc::now() + Duration::seconds(300)
    }

    #[test]
    fn sign_then_verify_round_trips_put_and_get() {
        let auth = cfg();
        for method in [Method::PUT, Method::GET] {
            let req = PresignRequest {
                method: method.clone(),
                bucket: "photos".into(),
                key: "a/b.txt".into(),
                expires_at: future(),
            };
            let url = sign(&auth, &req).expect("sign");
            let q = url.path_and_query.split_once('?').expect("query").1;
            let params = parse_presign_params(Some(q)).expect("parse");
            verify(&auth, &method, "photos", "a/b.txt", &params, Utc::now()).expect("verify");
        }
    }

    #[test]
    fn expired_params_are_access_denied() {
        let auth = cfg();
        let expires_at = Utc::now() - Duration::seconds(1);
        let expires = expires_at.timestamp();
        let canonical = canonical_string(&Method::GET, "b", "k", expires);
        let signature = hmac_hex(&auth.secret_access_key, &canonical).unwrap();
        let params = PresignParams { expires, signature };
        let err = verify(&auth, &Method::GET, "b", "k", &params, Utc::now()).unwrap_err();
        assert!(matches!(err, AppError::AccessDenied));
    }

    #[test]
    fn tampered_signature_is_access_denied() {
        let auth = cfg();
        let req = PresignRequest {
            method: Method::PUT,
            bucket: "b".into(),
            key: "k".into(),
            expires_at: future(),
        };
        let url = sign(&auth, &req).unwrap();
        let q = url.path_and_query.split_once('?').unwrap().1;
        let mut params = parse_presign_params(Some(q)).unwrap();
        params.signature.push('0');
        assert!(matches!(
            verify(&auth, &Method::PUT, "b", "k", &params, Utc::now()),
            Err(AppError::AccessDenied)
        ));
    }

    #[test]
    fn wrong_method_bucket_or_key_is_access_denied() {
        let auth = cfg();
        let req = PresignRequest {
            method: Method::PUT,
            bucket: "b".into(),
            key: "k".into(),
            expires_at: future(),
        };
        let url = sign(&auth, &req).unwrap();
        let params =
            parse_presign_params(Some(url.path_and_query.split_once('?').unwrap().1)).unwrap();
        assert!(matches!(
            verify(&auth, &Method::GET, "b", "k", &params, Utc::now()),
            Err(AppError::AccessDenied)
        ));
        assert!(matches!(
            verify(&auth, &Method::PUT, "other", "k", &params, Utc::now()),
            Err(AppError::AccessDenied)
        ));
        assert!(matches!(
            verify(&auth, &Method::PUT, "b", "other", &params, Utc::now()),
            Err(AppError::AccessDenied)
        ));
    }

    #[test]
    fn missing_query_params_are_access_denied() {
        assert!(matches!(
            parse_presign_params(None),
            Err(AppError::AccessDenied)
        ));
        assert!(matches!(
            parse_presign_params(Some("expires=1")),
            Err(AppError::AccessDenied)
        ));
        assert!(matches!(
            parse_presign_params(Some("signature=abcd")),
            Err(AppError::AccessDenied)
        ));
        assert!(matches!(
            params_from_parts(None, Some("ab")),
            Err(AppError::AccessDenied)
        ));
    }

    #[test]
    fn access_denied_message_does_not_contain_secret() {
        let auth = cfg();
        let err = AppError::AccessDenied;
        let msg = err.to_string();
        assert!(!msg.contains(&auth.secret_access_key));
        assert_eq!(msg, "access denied");
    }

    #[test]
    fn sign_rejects_past_expiry() {
        let auth = cfg();
        let req = PresignRequest {
            method: Method::GET,
            bucket: "b".into(),
            key: "k".into(),
            expires_at: Utc::now() - Duration::seconds(5),
        };
        assert!(matches!(
            sign(&auth, &req),
            Err(AppError::InvalidRequest(_))
        ));
    }

    #[test]
    fn access_credentials_accept_id_colon_secret_and_secret_alone() {
        let auth = cfg();
        let paired = format!("Bearer {}:{}", auth.access_key_id, auth.secret_access_key);
        assert!(access_credentials_match(&auth, Some(&paired)));
        assert!(access_credentials_match(
            &auth,
            Some(&format!("Bearer {}", auth.secret_access_key))
        ));
        assert!(!access_credentials_match(&auth, Some("Bearer wrong")));
        assert!(!access_credentials_match(
            &auth,
            Some("Bearer other:test-secret-do-not-use-in-prod")
        ));
        assert!(!access_credentials_match(&auth, None));
    }

    #[test]
    fn authorize_object_accepts_credentials_without_presign_query() {
        let auth = cfg();
        let bearer = format!("Bearer {}", auth.credential_token());
        authorize_object(
            &auth,
            &ObjectAuthRequest {
                method: &Method::PUT,
                bucket: "docs",
                key: "a.txt",
                expires: None,
                signature: None,
                authorization: Some(&bearer),
                now: Utc::now(),
            },
        )
        .expect("credential path");
    }

    #[test]
    fn authorize_object_prefers_presign_when_query_present() {
        let auth = cfg();
        // Partial presign query must not fall through to a valid Bearer.
        let bearer = format!("Bearer {}", auth.credential_token());
        assert!(matches!(
            authorize_object(
                &auth,
                &ObjectAuthRequest {
                    method: &Method::PUT,
                    bucket: "docs",
                    key: "a.txt",
                    expires: Some(Utc::now().timestamp() + 60),
                    signature: None,
                    authorization: Some(&bearer),
                    now: Utc::now(),
                },
            ),
            Err(AppError::AccessDenied)
        ));
    }
}
