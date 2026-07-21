//! Black-box acceptance tests for **chunk-level CDC dedup** (From the field).
//!
//! Drives the real axum router with CDC **enabled** on [`AppState`]. Unit tests
//! already cover the cutter / manifest / `stream_cdc_to_store`; these prove the
//! S3 surface actually stores manifests, reassembles on GET, shares disk for
//! near-duplicates, and keeps shared chunks across GC.
//!
//! ## Done when ALL true
//!
//! - [x] **PUT/GET round-trip.** Logical bytes survive; ETag is `hex(md5(bytes))`.
//!   *Proof: `put_get_round_trips_with_logical_etag`.*
//! - [x] **Range GET.** `Range: bytes=a-b` → 206 + exact slice via manifest assembly.
//!   *Proof: `range_get_returns_exact_slice`.*
//! - [x] **HEAD reports logical size.** `Content-Length` is the object size, not
//!   the manifest file size. *Proof: `head_reports_logical_content_length`.*
//! - [x] **Near-dupes share most on-disk bytes (SPEC).** Two large objects differing
//!   by a one-byte prefix do not nearly-double blob count.
//!   *Proof: `near_duplicates_share_most_on_disk_bytes`.*
//! - [x] **Identical bodies dedup.** Two keys, same bytes → same ETag; blob count
//!   does not double. *Proof: `identical_puts_dedup_manifest_and_chunks`.*
//! - [x] **GC keeps shared chunks.** Delete one near-dupe key, run GC; the other
//!   key's GET still round-trips. *Proof: `gc_keeps_chunks_shared_by_survivor`.*

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use md5::{Digest as _, Md5};
use tempfile::TempDir;
use tower::ServiceExt;

use object_store::cdc::CdcConfig;
use object_store::index_backend::IndexBackend;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};

/// FastCDC floors, small targets so ~8–16 KiB bodies still cut into multiple chunks.
fn test_cdc_config() -> CdcConfig {
    CdcConfig {
        enabled: true,
        min_size: 64,
        avg_size: 256,
        max_size: 1024,
        min_object_size: 0,
    }
}

struct TestApp {
    _dir: TempDir,
    root: std::path::PathBuf,
    router: Router,
    /// Same backend the router uses — for GC on the shared data dir.
    index: Arc<IndexBackend>,
}

struct Resp {
    status: StatusCode,
    headers: axum::http::HeaderMap,
    body: bytes::Bytes,
}

impl TestApp {
    fn new() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let root = dir.path().to_path_buf();
        let state = AppState::open(&root, DEFAULT_MAX_OBJECT_SIZE)
            .expect("open store stack")
            .with_cdc(test_cdc_config());
        let index = state.index.clone();
        let router = routes::router(state);
        Self {
            _dir: dir,
            root,
            router,
            index,
        }
    }

    async fn send(&self, req: Request<Body>) -> Resp {
        let res = self.router.clone().oneshot(req).await.expect("infallible");
        let status = res.status();
        let headers = res.headers().clone();
        let body = res.into_body().collect().await.expect("body").to_bytes();
        Resp {
            status,
            headers,
            body,
        }
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
            .header(header::CONTENT_TYPE, "application/octet-stream")
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

    async fn get_object_range(&self, bucket: &str, key: &str, range: &str) -> Resp {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}/{key}"))
            .header(header::RANGE, range)
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    async fn head_object(&self, bucket: &str, key: &str) -> Resp {
        let req = Request::builder()
            .method("HEAD")
            .uri(format!("/{bucket}/{key}"))
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    async fn delete_object(&self, bucket: &str, key: &str) -> Resp {
        let req = Request::builder()
            .method("DELETE")
            .uri(format!("/{bucket}/{key}"))
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    fn committed_blob_count(&self) -> usize {
        fn walk(dir: &std::path::Path, count: &mut usize) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    walk(&path, count);
                } else if path.is_file() {
                    *count += 1;
                }
            }
        }
        let mut count = 0;
        walk(&self.root.join("objects"), &mut count);
        count
    }

    async fn gc(&self) -> u64 {
        self.index.gc().await.expect("gc")
    }
}

fn expected_etag(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(bytes))
}

/// ~16 KiB of patterned bytes — large enough for multiple FastCDC cuts at test sizes.
fn large_payload() -> Vec<u8> {
    (0..16_384u32).map(|i| (i % 251) as u8).collect()
}

fn header_str(resp: &Resp, name: axum::http::HeaderName) -> &str {
    resp.headers
        .get(name)
        .expect("header present")
        .to_str()
        .expect("header ascii")
}

#[tokio::test]
async fn put_get_round_trips_with_logical_etag() {
    let app = TestApp::new();
    assert_eq!(app.create_bucket("cdc").await.status, StatusCode::OK);

    let payload = large_payload();
    let put = app.put_object("cdc", "v1.bin", &payload).await;
    assert_eq!(put.status, StatusCode::OK);
    assert_eq!(header_str(&put, header::ETAG), expected_etag(&payload));

    let get = app.get_object("cdc", "v1.bin").await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], &payload[..]);
    assert_eq!(header_str(&get, header::ETAG), expected_etag(&payload));
    assert_eq!(
        header_str(&get, header::CONTENT_LENGTH),
        payload.len().to_string()
    );
    // CDC should have produced more than one CAS object (chunks + manifest).
    assert!(
        app.committed_blob_count() >= 2,
        "expected manifest + ≥1 chunk, got {}",
        app.committed_blob_count()
    );
}

#[tokio::test]
async fn range_get_returns_exact_slice() {
    let app = TestApp::new();
    app.create_bucket("cdc").await;
    let payload = large_payload();
    app.put_object("cdc", "range.bin", &payload).await;

    let resp = app
        .get_object_range("cdc", "range.bin", "bytes=100-250")
        .await;
    assert_eq!(resp.status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(&resp.body[..], &payload[100..=250]);
    assert_eq!(header_str(&resp, header::CONTENT_LENGTH), "151");
}

#[tokio::test]
async fn head_reports_logical_content_length() {
    let app = TestApp::new();
    app.create_bucket("cdc").await;
    let payload = large_payload();
    app.put_object("cdc", "head.bin", &payload).await;

    let head = app.head_object("cdc", "head.bin").await;
    assert_eq!(head.status, StatusCode::OK);
    assert!(head.body.is_empty(), "HEAD must not return a body");
    assert_eq!(
        header_str(&head, header::CONTENT_LENGTH),
        payload.len().to_string(),
        "Content-Length must be logical size, not manifest JSON size"
    );
    assert_eq!(header_str(&head, header::ETAG), expected_etag(&payload));
}

#[tokio::test]
async fn near_duplicates_share_most_on_disk_bytes() {
    let app = TestApp::new();
    app.create_bucket("cdc").await;

    let base = large_payload();
    let put_a = app.put_object("cdc", "v1.bin", &base).await;
    assert_eq!(put_a.status, StatusCode::OK);
    let after_first = app.committed_blob_count();
    assert!(
        after_first >= 2,
        "first object must leave chunks + manifest"
    );

    let mut edited = Vec::with_capacity(base.len() + 1);
    edited.push(0xFF);
    edited.extend_from_slice(&base);
    let put_b = app.put_object("cdc", "v2.bin", &edited).await;
    assert_eq!(put_b.status, StatusCode::OK);
    let after_second = app.committed_blob_count();

    // A full second copy would be ~2× first (plus a second manifest). CDC must
    // land well below that — only a few new chunk files + one new manifest.
    let naive_second_copy = after_first * 2;
    assert!(
        after_second < naive_second_copy,
        "near-dupe must share chunks: after_first={after_first} after_second={after_second} \
         (naive double would be {naive_second_copy})"
    );
    // Growth should be a small fraction of the first object's footprint.
    let growth = after_second - after_first;
    assert!(
        growth <= after_first / 2 + 2,
        "expected modest growth from a 1-byte prefix insert; growth={growth} first={after_first}"
    );

    // Both keys still readable.
    assert_eq!(&app.get_object("cdc", "v1.bin").await.body[..], &base[..]);
    assert_eq!(&app.get_object("cdc", "v2.bin").await.body[..], &edited[..]);
}

#[tokio::test]
async fn identical_puts_dedup_manifest_and_chunks() {
    let app = TestApp::new();
    app.create_bucket("cdc").await;
    let payload = large_payload();

    let a = app.put_object("cdc", "a.bin", &payload).await;
    let after_a = app.committed_blob_count();
    let b = app.put_object("cdc", "b.bin", &payload).await;
    let after_b = app.committed_blob_count();

    assert_eq!(a.status, StatusCode::OK);
    assert_eq!(b.status, StatusCode::OK);
    assert_eq!(header_str(&a, header::ETAG), header_str(&b, header::ETAG));
    assert_eq!(
        after_a, after_b,
        "identical content must not allocate a second set of CAS blobs"
    );
}

#[tokio::test]
async fn gc_keeps_chunks_shared_by_survivor() {
    let app = TestApp::new();
    app.create_bucket("cdc").await;

    let base = large_payload();
    let mut edited = Vec::with_capacity(base.len() + 1);
    edited.push(0xAB);
    edited.extend_from_slice(&base);

    app.put_object("cdc", "keep.bin", &base).await;
    app.put_object("cdc", "drop.bin", &edited).await;
    let before_delete = app.committed_blob_count();

    assert_eq!(
        app.delete_object("cdc", "drop.bin").await.status,
        StatusCode::NO_CONTENT
    );
    let _reclaimed = app.gc().await;

    let get = app.get_object("cdc", "keep.bin").await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], &base[..]);

    // Survivor still needs its chunks + manifest; count must not collapse to 0.
    assert!(
        app.committed_blob_count() >= 2,
        "GC must not reap the survivor's chunks; before_delete={before_delete} after={}",
        app.committed_blob_count()
    );
}
