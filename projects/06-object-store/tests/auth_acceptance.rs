//! Black-box end-to-end tests for dual-path object auth.
//!
//! These drive the real axum router — the same path a client hits over the
//! wire — with auth enabled via [`AppState::with_auth`]. Unit tests in
//! `src/auth.rs` and `src/routes.rs` cover the HMAC brain and a few happy
//! paths; this file is the acceptance surface for the SPEC security box:
//!
//! ## Done when ALL true
//!
//! - [ ] **Unsigned object writes are rejected.** With auth installed, a bare
//!   `PUT /{bucket}/{key}` returns 403. *Proof: `unsigned_put_is_forbidden`.*
//! - [ ] **Access credentials authorize the normal path.** Bearer
//!   `ACCESS_KEY_ID:SECRET` (or secret alone) can PUT then GET the same object.
//!   *Proof: `credential_put_get_round_trip`, `secret_alone_bearer_works`.*
//! - [ ] **Wrong credentials are rejected.** A bad Bearer is 403 and the body
//!   never echoes the real secret. *Proof: `wrong_credentials_are_forbidden`.*
//! - [ ] **Presign mints a scoped URL.** `POST /presign` with valid Bearer
//!   returns a URL that can PUT without further credentials; GET needs its own
//!   signed URL or Bearer. *Proof: `presign_put_then_credential_get`.*
//! - [ ] **Scope is enforced.** A PUT-signed URL cannot GET; a URL for key A
//!   cannot touch key B; an expired URL is 403.
//!   *Proof: `presigned_url_is_method_and_key_scoped`, `expired_presigned_url_is_forbidden`.*
//! - [ ] **Partial presign query does not fall through to Bearer.**
//!   *Proof: `partial_presign_query_ignores_valid_bearer`.*
//! - [ ] **Control plane stays open.** `/healthz` and `PUT /{bucket}` work
//!   without credentials when auth is on. *Proof: `control_plane_stays_open`.*
//! - [ ] **Auth off means open API.** Without `with_auth`, unsigned PUT/GET
//!   still work. *Proof: `auth_disabled_keeps_api_open`.*

use axum::body::Body;
use axum::http::header::{AUTHORIZATION, CONTENT_TYPE};
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::{Duration, Utc};
use hmac::{Hmac, Mac};
use http_body_util::BodyExt;
use object_store::auth::{AuthConfig, PresignRequest, QUERY_EXPIRES, QUERY_SIGNATURE};
use object_store::s3_xml::parse_error;
use object_store::{auth, routes, AppState, DEFAULT_MAX_OBJECT_SIZE};
use sha2::Sha256;
use tempfile::TempDir;
use tower::ServiceExt;

type HmacSha256 = Hmac<Sha256>;

struct TestApp {
    _dir: TempDir,
    router: Router,
    auth: AuthConfig,
}

struct Resp {
    status: StatusCode,
    body: bytes::Bytes,
}

impl Resp {
    fn error_message(&self) -> String {
        parse_error(&self.body)
            .expect("response body should be valid Error XML")
            .message
    }

    fn error_code(&self) -> String {
        parse_error(&self.body)
            .expect("response body should be valid Error XML")
            .code
    }
}

impl TestApp {
    fn with_auth() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let auth = AuthConfig::new("e2e-akid", "e2e-test-secret-do-not-use");
        let state = AppState::open(dir.path(), DEFAULT_MAX_OBJECT_SIZE)
            .expect("open store stack")
            .with_auth(Some(auth.clone()));
        Self {
            _dir: dir,
            router: routes::router(state),
            auth,
        }
    }

    fn open() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let auth = AuthConfig::new("unused", "unused");
        let state = AppState::open(dir.path(), DEFAULT_MAX_OBJECT_SIZE).expect("open store stack");
        Self {
            _dir: dir,
            router: routes::router(state),
            auth,
        }
    }

    fn bearer(&self) -> String {
        format!("Bearer {}", self.auth.credential_token())
    }

    async fn send(&self, req: Request<Body>) -> Resp {
        let res = self.router.clone().oneshot(req).await.expect("infallible");
        let status = res.status();
        let body = res.into_body().collect().await.expect("body").to_bytes();
        Resp { status, body }
    }

    async fn create_bucket(&self, bucket: &str) -> Resp {
        self.send(
            Request::builder()
                .method("PUT")
                .uri(format!("/{bucket}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    async fn put_raw(&self, uri: &str, body: &[u8], authorization: Option<&str>) -> Resp {
        let mut req = Request::builder()
            .method("PUT")
            .uri(uri)
            .header(CONTENT_TYPE, "text/plain");
        if let Some(authz) = authorization {
            req = req.header(AUTHORIZATION, authz);
        }
        self.send(req.body(Body::from(body.to_vec())).unwrap())
            .await
    }

    async fn get_raw(&self, uri: &str, authorization: Option<&str>) -> Resp {
        let mut req = Request::builder().method("GET").uri(uri);
        if let Some(authz) = authorization {
            req = req.header(AUTHORIZATION, authz);
        }
        self.send(req.body(Body::empty()).unwrap()).await
    }

    async fn delete_raw(&self, uri: &str, authorization: Option<&str>) -> Resp {
        let mut req = Request::builder().method("DELETE").uri(uri);
        if let Some(authz) = authorization {
            req = req.header(AUTHORIZATION, authz);
        }
        self.send(req.body(Body::empty()).unwrap()).await
    }

    async fn presign(
        &self,
        method: &str,
        bucket: &str,
        key: &str,
        expires_in_secs: u64,
        authorization: Option<&str>,
    ) -> Resp {
        let body = serde_json::json!({
            "method": method,
            "bucket": bucket,
            "key": key,
            "expires_in_secs": expires_in_secs,
        });
        let mut req = Request::builder()
            .method("POST")
            .uri("/presign")
            .header(CONTENT_TYPE, "application/json");
        if let Some(authz) = authorization {
            req = req.header(AUTHORIZATION, authz);
        }
        self.send(
            req.body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
    }
}

/// Forge a previously-valid URL whose `expires` is already in the past.
///
/// [`auth::sign`] refuses past expiry (correct for minting), so the e2e suite
/// builds the same canonical string the server verifies.
fn forged_expired_url(secret: &str, method: &str, bucket: &str, key: &str) -> String {
    let expires = Utc::now().timestamp() - 30;
    let canonical = format!("{method}\n/{bucket}/{key}\n{expires}");
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes()).expect("hmac accepts any byte key");
    mac.update(canonical.as_bytes());
    let signature = hex::encode(mac.finalize().into_bytes());
    format!("/{bucket}/{key}?{QUERY_EXPIRES}={expires}&{QUERY_SIGNATURE}={signature}")
}

#[tokio::test]
async fn unsigned_put_is_forbidden() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let resp = app.put_raw("/docs/a.txt", b"nope", None).await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
    assert_eq!(resp.error_code(), "AccessDenied");
}

#[tokio::test]
async fn unsigned_get_is_forbidden() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);
    assert_eq!(
        app.put_raw("/docs/a.txt", b"secret-bytes", Some(&app.bearer()))
            .await
            .status,
        StatusCode::OK
    );

    let resp = app.get_raw("/docs/a.txt", None).await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
    assert_eq!(resp.error_code(), "AccessDenied");
}

#[tokio::test]
async fn credential_put_get_round_trip() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let put = app
        .put_raw("/docs/cred.txt", b"via-creds", Some(&app.bearer()))
        .await;
    assert_eq!(put.status, StatusCode::OK);

    let get = app.get_raw("/docs/cred.txt", Some(&app.bearer())).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], b"via-creds");
}

#[tokio::test]
async fn secret_alone_bearer_works() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let bearer = format!("Bearer {}", app.auth.secret_access_key);
    assert_eq!(
        app.put_raw("/docs/plain.txt", b"ok", Some(&bearer))
            .await
            .status,
        StatusCode::OK
    );
    let get = app.get_raw("/docs/plain.txt", Some(&bearer)).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], b"ok");
}

#[tokio::test]
async fn wrong_credentials_are_forbidden() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let resp = app
        .put_raw(
            "/docs/a.txt",
            b"nope",
            Some("Bearer e2e-akid:totally-wrong-secret"),
        )
        .await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
    assert_eq!(resp.error_code(), "AccessDenied");
    let msg = resp.error_message();
    assert!(!msg.contains(&app.auth.secret_access_key));
    assert_eq!(msg, "access denied");
}

#[tokio::test]
async fn credential_delete_works() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);
    assert_eq!(
        app.put_raw("/docs/gone.txt", b"temp", Some(&app.bearer()))
            .await
            .status,
        StatusCode::OK
    );

    assert_eq!(
        app.delete_raw("/docs/gone.txt", Some(&app.bearer()))
            .await
            .status,
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        app.get_raw("/docs/gone.txt", Some(&app.bearer()))
            .await
            .status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn presign_without_bearer_is_forbidden() {
    let app = TestApp::with_auth();
    let resp = app.presign("PUT", "docs", "a.txt", 60, None).await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn presign_with_wrong_bearer_is_forbidden() {
    let app = TestApp::with_auth();
    let resp = app
        .presign("PUT", "docs", "a.txt", 60, Some("Bearer wrong:creds"))
        .await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn presign_put_then_credential_get() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let minted = app
        .presign("PUT", "docs", "signed.txt", 120, Some(&app.bearer()))
        .await;
    assert_eq!(minted.status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_slice(&minted.body).expect("json");
    let put_url = json["url"].as_str().expect("url field");
    assert!(put_url.contains("expires="));
    assert!(put_url.contains("signature="));

    // Presigned PUT — no Authorization header.
    assert_eq!(
        app.put_raw(put_url, b"from-presign", None).await.status,
        StatusCode::OK
    );

    // Read back with long-lived credentials (GET was never presigned).
    let get = app.get_raw("/docs/signed.txt", Some(&app.bearer())).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], b"from-presign");
}

#[tokio::test]
async fn presigned_get_round_trips_after_credential_put() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);
    assert_eq!(
        app.put_raw("/docs/read-me.txt", b"payload", Some(&app.bearer()))
            .await
            .status,
        StatusCode::OK
    );

    let minted = app
        .presign("GET", "docs", "read-me.txt", 120, Some(&app.bearer()))
        .await;
    assert_eq!(minted.status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_slice(&minted.body).expect("json");
    let get_url = json["url"].as_str().expect("url");

    let get = app.get_raw(get_url, None).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], b"payload");
}

#[tokio::test]
async fn presigned_url_is_method_and_key_scoped() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let put_signed = auth::sign(
        &app.auth,
        &PresignRequest {
            method: axum::http::Method::PUT,
            bucket: "docs".into(),
            key: "only-put.txt".into(),
            expires_at: Utc::now() + Duration::seconds(120),
        },
    )
    .expect("sign put");

    assert_eq!(
        app.put_raw(&put_signed.path_and_query, b"bytes", None)
            .await
            .status,
        StatusCode::OK
    );

    // Same signature, wrong method → 403.
    let get_with_put_sig = app.get_raw(&put_signed.path_and_query, None).await;
    assert_eq!(get_with_put_sig.status, StatusCode::FORBIDDEN);

    // Same signature shape, different key → 403.
    let other_key_uri =
        put_signed
            .path_and_query
            .replacen("/docs/only-put.txt", "/docs/other.txt", 1);
    assert_eq!(
        app.put_raw(&other_key_uri, b"bytes", None).await.status,
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn expired_presigned_url_is_forbidden() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    let url = forged_expired_url(&app.auth.secret_access_key, "PUT", "docs", "late.txt");
    let resp = app.put_raw(&url, b"too-late", None).await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
    assert_eq!(resp.error_code(), "AccessDenied");
}

#[tokio::test]
async fn partial_presign_query_ignores_valid_bearer() {
    let app = TestApp::with_auth();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    // expires present, signature missing — must not fall through to Bearer.
    let uri = format!(
        "/docs/partial.txt?{}={}",
        QUERY_EXPIRES,
        Utc::now().timestamp() + 60
    );
    let resp = app.put_raw(&uri, b"nope", Some(&app.bearer())).await;
    assert_eq!(resp.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn control_plane_stays_open() {
    let app = TestApp::with_auth();

    let health = app
        .send(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(health.status, StatusCode::OK);
    assert_eq!(&health.body[..], b"ok");

    // Bucket create is outside the object middleware.
    assert_eq!(
        app.create_bucket("open-bucket").await.status,
        StatusCode::OK
    );

    // Listing a bucket is also outside object middleware.
    let list = app
        .send(
            Request::builder()
                .uri("/open-bucket")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(list.status, StatusCode::OK);
}

#[tokio::test]
async fn auth_disabled_keeps_api_open() {
    let app = TestApp::open();
    assert_eq!(app.create_bucket("docs").await.status, StatusCode::OK);

    assert_eq!(
        app.put_raw("/docs/open.txt", b"wide-open", None)
            .await
            .status,
        StatusCode::OK
    );
    let get = app.get_raw("/docs/open.txt", None).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], b"wide-open");
}
