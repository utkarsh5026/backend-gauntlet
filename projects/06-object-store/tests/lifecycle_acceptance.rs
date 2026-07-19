//! Black-box acceptance tests for the **lifecycle** vertical (expire / tier).
//!
//! These drive the real axum router for all object operations and assertions —
//! a client only ever sees HTTP. The one seam they reach past HTTP for is
//! *triggering a sweep at a chosen instant*: lifecycle is a background daemon,
//! not a client verb, so a test builds a [`Lifecycle`] over the **same data
//! dir** and calls [`Lifecycle::run_once_at`] with a simulated `now`. Because
//! the filesystem *is* the store, that engine and the router's `AppState` see
//! identical state. No sleeping, no backdating files: objects are PUT "now" and
//! the sweep runs "in the future".
//!
//! ## Done when ALL true  (derived — lifecycle has no graded SPEC block)
//!
//! - [ ] **Expiration removes the object.** Under an `expire_after_days` rule, a
//!   swept-past-age object is gone: GET returns 404.
//!   *Proof: `expired_object_becomes_not_found`.*
//! - [ ] **A prefix rule only touches matching keys.** A non-matching key in the
//!   same bucket survives the same sweep.
//!   *Proof: `prefix_rule_leaves_non_matching_keys_untouched`.*
//! - [ ] **No rule ages nothing.** A bucket with no policy is untouched even by a
//!   sweep far in the future.
//!   *Proof: `bucket_without_a_policy_ages_nothing`.*
//! - [ ] **Sweeps are idempotent.** Running the same sweep twice yields the same
//!   observable state — no double-delete error, no corruption.
//!   *Proof: `sweeping_twice_is_idempotent`.*
//! - [ ] **Tiering is transparent.** Under a `tier_after_days` rule, a swept
//!   object's bytes are cold-compressed yet GET returns them byte-for-byte —
//!   the client cannot tell. *Proof: `tiered_object_still_round_trips`.*

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use http_body_util::BodyExt;
use tempfile::TempDir;
use tower::ServiceExt;

use object_store::index::Index;
use object_store::index_backend::IndexBackend;
use object_store::lifecycle::{Lifecycle, LifecyclePolicy, LifecycleRule};
use object_store::store::Store;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};

/// HTTP surface + a handle to the same data dir the sweeper will operate on.
struct TestApp {
    _dir: TempDir,
    root: std::path::PathBuf,
    router: Router,
}

struct Resp {
    status: StatusCode,
    body: bytes::Bytes,
}

impl TestApp {
    fn new() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let root = dir.path().to_path_buf();
        let state = AppState::open(&root, DEFAULT_MAX_OBJECT_SIZE).expect("open store stack");
        let router = routes::router(state);
        Self {
            _dir: dir,
            root,
            router,
        }
    }

    async fn send(&self, req: Request<Body>) -> Resp {
        let res = self.router.clone().oneshot(req).await.expect("infallible");
        let status = res.status();
        let body = res.into_body().collect().await.expect("body").to_bytes();
        Resp { status, body }
    }

    async fn create_bucket(&self, bucket: &str) -> Resp {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}"))
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    async fn put_object(&self, bucket: &str, key: &str, body: &[u8]) -> Resp {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}/{key}"))
            .header("content-type", "application/octet-stream")
            .body(Body::from(body.to_vec()))
            .unwrap();
        self.send(req).await
    }

    async fn get_object(&self, bucket: &str, key: &str) -> Resp {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}/{key}"))
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    /// Build a lifecycle engine over the *same* data dir the router uses, so a
    /// sweep sees exactly what clients wrote. This is the one non-HTTP seam —
    /// the sweep is a daemon, not a client verb.
    fn engine(&self) -> Arc<Lifecycle> {
        let store = Store::open(&self.root).expect("open store");
        let index = Index::open(&self.root, store.clone()).expect("open index");
        Lifecycle::new(Arc::new(IndexBackend::local(index)), store)
    }

    /// `PUT /{bucket}?lifecycle` — set the policy over the real HTTP surface.
    async fn set_policy(&self, bucket: &str, policy: &LifecyclePolicy) -> Resp {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}?lifecycle"))
            .body(Body::from(serde_json::to_vec(policy).unwrap()))
            .unwrap();
        self.send(req).await
    }

    /// `GET /{bucket}?lifecycle` — read the policy back.
    async fn get_policy(&self, bucket: &str) -> Resp {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}?lifecycle"))
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }
}

/// A single-action rule (the rest of the fields defaulted off).
fn rule(
    prefix: Option<&str>,
    expire_after_days: Option<u32>,
    tier_after_days: Option<u32>,
) -> LifecycleRule {
    LifecycleRule {
        id: "acceptance".into(),
        enabled: true,
        prefix: prefix.map(str::to_string),
        tier_after_days,
        expire_after_days,
        noncurrent_expire_after_days: None,
        abort_multipart_after_days: None,
    }
}

fn policy(rules: Vec<LifecycleRule>) -> LifecyclePolicy {
    LifecyclePolicy { rules }
}

/// `days` in the future from real now — the simulated sweep instant.
fn in_days(days: i64) -> DateTime<Utc> {
    Utc::now() + ChronoDuration::days(days)
}

// ---------------------------------------------------------------------------

#[tokio::test]
async fn expired_object_becomes_not_found() {
    let app = TestApp::new();
    let lifecycle = app.engine();

    app.create_bucket("photos").await;
    app.put_object("photos", "old.jpg", b"ancient bytes").await;
    app.set_policy("photos", &policy(vec![rule(None, Some(30), None)]))
        .await;

    // Not yet aged: still present.
    assert_eq!(
        app.get_object("photos", "old.jpg").await.status,
        StatusCode::OK
    );

    lifecycle.run_once_at(in_days(31)).await.expect("sweep");

    assert_eq!(
        app.get_object("photos", "old.jpg").await.status,
        StatusCode::NOT_FOUND,
        "an expired object must read as gone"
    );
}

#[tokio::test]
async fn prefix_rule_leaves_non_matching_keys_untouched() {
    let app = TestApp::new();
    let lifecycle = app.engine();

    app.create_bucket("data").await;
    app.put_object("data", "tmp/scratch", b"disposable").await;
    app.put_object("data", "keep/forever", b"precious").await;
    // Only `tmp/` expires.
    app.set_policy("data", &policy(vec![rule(Some("tmp/"), Some(1), None)]))
        .await;

    lifecycle.run_once_at(in_days(365)).await.expect("sweep");

    assert_eq!(
        app.get_object("data", "tmp/scratch").await.status,
        StatusCode::NOT_FOUND,
        "matching prefix expires"
    );
    assert_eq!(
        app.get_object("data", "keep/forever").await.status,
        StatusCode::OK,
        "non-matching key must survive"
    );
}

#[tokio::test]
async fn bucket_without_a_policy_ages_nothing() {
    let app = TestApp::new();
    let lifecycle = app.engine();

    app.create_bucket("vault").await;
    app.put_object("vault", "receipt.pdf", b"keep me").await;
    // No set_policy call: empty policy.

    lifecycle
        .run_once_at(in_days(100_000))
        .await
        .expect("sweep");

    let got = app.get_object("vault", "receipt.pdf").await;
    assert_eq!(got.status, StatusCode::OK);
    assert_eq!(&got.body[..], b"keep me");
}

#[tokio::test]
async fn sweeping_twice_is_idempotent() {
    let app = TestApp::new();
    let lifecycle = app.engine();

    app.create_bucket("logs").await;
    app.put_object("logs", "a.log", b"line").await;
    app.put_object("logs", "b.log", b"line").await;
    app.set_policy("logs", &policy(vec![rule(None, Some(7), None)]))
        .await;

    let now = in_days(30);
    lifecycle.run_once_at(now).await.expect("first sweep");
    // The second sweep re-encounters already-expired keys: it must not error.
    lifecycle
        .run_once_at(now)
        .await
        .expect("second sweep is a no-op");

    assert_eq!(
        app.get_object("logs", "a.log").await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        app.get_object("logs", "b.log").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn tiered_object_still_round_trips() {
    let app = TestApp::new();
    let lifecycle = app.engine();

    app.create_bucket("cold").await;
    let payload = b"the quick brown fox ".repeat(500); // compressible
    app.put_object("cold", "archive.txt", &payload).await;
    app.set_policy("cold", &policy(vec![rule(None, None, Some(30))]))
        .await;

    lifecycle.run_once_at(in_days(400)).await.expect("sweep");

    // The bytes are now zstd-compressed on disk, but the client sees plaintext.
    let got = app.get_object("cold", "archive.txt").await;
    assert_eq!(got.status, StatusCode::OK);
    assert_eq!(
        got.body.as_ref(),
        payload.as_slice(),
        "tiering must be transparent"
    );
}

#[tokio::test]
async fn put_then_get_lifecycle_round_trips_the_policy() {
    let app = TestApp::new();
    app.create_bucket("configured").await;

    let mut r = rule(Some("logs/"), Some(365), Some(30));
    r.id = "cool-then-delete".into();
    assert_eq!(
        app.set_policy("configured", &policy(vec![r])).await.status,
        StatusCode::OK
    );

    let got = app.get_policy("configured").await;
    assert_eq!(got.status, StatusCode::OK);
    let read: LifecyclePolicy = serde_json::from_slice(&got.body).expect("policy JSON");
    assert_eq!(read.rules.len(), 1);
    assert_eq!(read.rules[0].id, "cool-then-delete");
    assert_eq!(read.rules[0].prefix.as_deref(), Some("logs/"));
    assert_eq!(read.rules[0].tier_after_days, Some(30));
    assert_eq!(read.rules[0].expire_after_days, Some(365));
}

#[tokio::test]
async fn put_lifecycle_rejects_an_incoherent_policy() {
    let app = TestApp::new();
    app.create_bucket("bad").await;

    // tier (100) >= expire (10): cooling something you're about to delete.
    let resp = app
        .set_policy("bad", &policy(vec![rule(None, Some(10), Some(100))]))
        .await;
    assert_eq!(resp.status, StatusCode::BAD_REQUEST);

    // And it must not have been persisted.
    let read: LifecyclePolicy =
        serde_json::from_slice(&app.get_policy("bad").await.body).expect("policy JSON");
    assert!(read.rules.is_empty(), "a rejected policy must not persist");
}

#[tokio::test]
async fn put_lifecycle_on_a_missing_bucket_is_not_found() {
    let app = TestApp::new();
    let resp = app
        .set_policy("ghost", &policy(vec![rule(None, Some(30), None)]))
        .await;
    assert_eq!(resp.status, StatusCode::NOT_FOUND);
}
