//! Black-box HTTP integration tests for the object store.
//!
//! Where the per-module unit tests (`src/*.rs`) poke each vertical in isolation,
//! these drive the *whole* stack through the real axum router — the exact code
//! path a client hits over the wire: route matching, query-param dispatch, body
//! streaming, header emission, and the `AppError → HTTP status` mapping. The
//! router runs in-process via `tower`'s `oneshot` (no socket, no port), against
//! a throwaway data dir that is wiped when each test's `TestApp` drops.
//!
//! Scope: V1 (content-addressed store), V2 (streaming PUT/GET + size cap), and
//! V3 (bucket namespace, prefix/delimiter listing, pagination). The V4 multipart
//! verbs (`?uploads`, `?uploadId&partNumber`) are deliberately NOT exercised —
//! their handlers still `todo!()` and would panic. Those belong to a `/quest`.

use axum::body::Body;
use axum::http::header::{CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, ETAG, IF_NONE_MATCH, RANGE};
use axum::http::{HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use md5::Md5;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};
use serde_json::Value;
use sha2::Digest as _;
use std::path::PathBuf;
use tempfile::TempDir;
use tower::ServiceExt; // ServiceExt::oneshot

struct TestApp {
    _dir: TempDir,
    root: PathBuf,
    router: Router,
}

/// The whole response, materialised: status, headers, and the fully-drained
/// body bytes (the store streams responses, so we collect them here).
struct Resp {
    status: StatusCode,
    headers: HeaderMap,
    body: bytes::Bytes,
}

impl Resp {
    /// Parse the body as JSON (list responses, error envelopes).
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).expect("response body should be valid JSON")
    }

    fn header(&self, name: axum::http::HeaderName) -> Option<String> {
        self.headers
            .get(name)
            .map(|v| v.to_str().expect("header is valid UTF-8").to_string())
    }
}

impl TestApp {
    fn new() -> Self {
        Self::with_max_size(DEFAULT_MAX_OBJECT_SIZE)
    }

    fn with_max_size(max_object_size: u64) -> Self {
        let dir = TempDir::new().expect("create temp data dir");
        let root = dir.path().to_path_buf();
        let state = AppState::open(&root, max_object_size).expect("open store stack");
        let router = routes::router(state);
        Self {
            _dir: dir,
            root,
            router,
        }
    }

    /// Send one request through the router. `oneshot` consumes the service, so
    /// we clone the (cheap, `Arc`-backed) router per call.
    async fn send(&self, req: Request<Body>) -> Resp {
        let res = self
            .router
            .clone()
            .oneshot(req)
            .await
            .expect("router is infallible");
        let status = res.status();
        let headers = res.headers().clone();
        let body = res
            .into_body()
            .collect()
            .await
            .expect("collect response body")
            .to_bytes();
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

    async fn put_object(&self, bucket: &str, key: &str, content_type: &str, body: &[u8]) -> Resp {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}/{key}"))
            .header(CONTENT_TYPE, content_type)
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

    /// GET carrying a `Range:` header, e.g. `range = "bytes=2-5"`.
    async fn get_object_range(&self, bucket: &str, key: &str, range: &str) -> Resp {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}/{key}"))
            .header(RANGE, range)
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    /// GET carrying an `If-None-Match:` header (conditional-request / ETag path).
    async fn get_object_if_none_match(&self, bucket: &str, key: &str, etag: &str) -> Resp {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}/{key}"))
            .header(IF_NONE_MATCH, etag)
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

    async fn list(&self, bucket: &str, query: &str) -> Resp {
        let uri = if query.is_empty() {
            format!("/{bucket}")
        } else {
            format!("/{bucket}?{query}")
        };
        let req = Request::builder()
            .method("GET")
            .uri(uri)
            .body(Body::empty())
            .unwrap();
        self.send(req).await
    }

    /// Count committed blobs on disk (`objects/ab/cd/<64-hex>`). Lets the dedup
    /// test assert the physical store, not just the HTTP responses.
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
}

/// The S3 single-PUT ETag: `hex(md5(bytes))`, no quotes, no `-N` suffix.
fn expected_single_put_etag(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(bytes))
}

#[tokio::test]
async fn healthz_returns_ok() {
    let app = TestApp::new();
    let req = Request::builder()
        .uri("/healthz")
        .body(Body::empty())
        .unwrap();
    let resp = app.send(req).await;
    assert_eq!(resp.status, StatusCode::OK);
    assert_eq!(&resp.body[..], b"ok");
}

#[tokio::test]
async fn put_then_get_round_trips_bytes_and_metadata() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "animals/fox.txt";
    app.create_bucket(bucket).await;

    let body = b"the quick brown fox jumps over the lazy dog";
    let put = app.put_object(bucket, key, "text/plain", body).await;
    assert_eq!(put.status, StatusCode::OK);
    assert_eq!(
        put.header(ETAG).as_deref(),
        Some(expected_single_put_etag(body).as_str()),
        "the PUT response ETag must be hex(md5(bytes))"
    );

    let get = app.get_object(bucket, key).await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(
        &get.body[..],
        body,
        "GET must return the exact stored bytes"
    );
    assert_eq!(
        get.header(CONTENT_TYPE).as_deref(),
        Some("text/plain"),
        "the stored Content-Type must round-trip"
    );
    assert_eq!(
        get.header(CONTENT_LENGTH).as_deref(),
        Some(body.len().to_string().as_str()),
        "Content-Length must be the object size"
    );
    assert_eq!(
        get.header(ETAG).as_deref(),
        Some(expected_single_put_etag(body).as_str()),
        "the GET ETag must match the PUT ETag"
    );
}

// ── Range GET (V2 byte ranges) ────────────────────────────────────────────────
//
// The handler parses `Range: bytes=<start>-<end>` (inclusive, both bounds
// required), streams exactly that slice, and answers `206 Partial Content` with a
// slice-sized `Content-Length` plus a `bytes <start>-<end>/<total>` Content-Range.
// A malformed or out-of-bounds range is a 400, never a 500.

#[tokio::test]
async fn range_get_preserves_the_whole_object_etag() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "digits.txt";
    app.create_bucket(bucket).await;

    let body = b"0123456789";
    app.put_object(bucket, key, "text/plain", body).await;

    // A partial read is still a read of the *same object*: the ETag identifies the
    // whole object (hex(md5(all bytes))), so ranging must not rewrite it to some
    // per-slice digest — otherwise conditional caching across ranges breaks.
    let get = app.get_object_range(bucket, key, "bytes=2-5").await;
    assert_eq!(get.status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(&get.body[..], b"2345");
    assert_eq!(
        get.header(ETAG).as_deref(),
        Some(expected_single_put_etag(body).as_str()),
        "the ETag must be the whole-object md5, unchanged by ranging"
    );
    assert_eq!(get.header(CONTENT_RANGE).as_deref(), Some("bytes 2-5/10"));
}

#[tokio::test]
async fn range_get_can_read_the_final_byte() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "digits.txt";
    app.create_bucket(bucket).await;

    let body = b"0123456789";
    app.put_object(bucket, key, "text/plain", body).await;

    // The last valid offset is len-1 (=9); bytes=9-9 is a one-byte range, not an
    // off-by-one out-of-bounds error.
    let get = app.get_object_range(bucket, key, "bytes=9-9").await;
    assert_eq!(get.status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(&get.body[..], b"9");
    assert_eq!(get.header(CONTENT_LENGTH).as_deref(), Some("1"));
    assert_eq!(get.header(CONTENT_RANGE).as_deref(), Some("bytes 9-9/10"));
}

#[tokio::test]
async fn range_get_past_end_of_object_is_400() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "digits.txt";
    app.create_bucket(bucket).await;
    app.put_object(bucket, key, "text/plain", b"0123456789")
        .await;

    // end=100 is well past the last byte (offset 9) → open_blob_range rejects it.
    let get = app.get_object_range(bucket, key, "bytes=0-100").await;
    assert_eq!(get.status, StatusCode::BAD_REQUEST);
    assert!(get.json()["error"]
        .as_str()
        .unwrap()
        .contains("invalid range"));
}

#[tokio::test]
async fn range_get_with_start_after_end_is_400() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "digits.txt";
    app.create_bucket(bucket).await;
    app.put_object(bucket, key, "text/plain", b"0123456789")
        .await;

    let get = app.get_object_range(bucket, key, "bytes=5-2").await;
    assert_eq!(get.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn malformed_range_header_is_400() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "digits.txt";
    app.create_bucket(bucket).await;
    app.put_object(bucket, key, "text/plain", b"0123456789")
        .await;

    // Wrong unit, missing '=', missing '-', and non-numeric bounds all fail the
    // header parser before the store is ever touched → 400, not 500.
    for bad in ["items=0-5", "bytes 0-5", "bytes=05", "bytes=a-b"] {
        let get = app.get_object_range(bucket, key, bad).await;
        assert_eq!(
            get.status,
            StatusCode::BAD_REQUEST,
            "malformed Range {bad:?} must be a 400"
        );
    }
}

// ── Conditional GET on the ETag (If-None-Match) ───────────────────────────────
//
// The matching-ETag → 304 case lives in `matching_if_none_match_returns_304…`;
// this covers the other branch — a stale ETag must fall through to a full 200.

#[tokio::test]
async fn if_none_match_with_stale_etag_returns_the_object() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "cached.txt";
    app.create_bucket(bucket).await;

    let body = b"cache me if you can";
    app.put_object(bucket, key, "text/plain", body).await;

    // The client's cached ETag no longer matches → full 200 with the new bytes.
    let get = app
        .get_object_if_none_match(bucket, key, "\"an-old-etag\"")
        .await;
    assert_eq!(get.status, StatusCode::OK);
    assert_eq!(&get.body[..], body);
    assert_eq!(
        get.header(ETAG).as_deref(),
        Some(expected_single_put_etag(body).as_str())
    );
}

#[tokio::test]
async fn range_get_returns_only_the_requested_inclusive_slice() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "sequence.txt";
    app.create_bucket(bucket).await;
    app.put_object(bucket, key, "text/plain", b"0123456789")
        .await;

    let get = app.get_object_range(bucket, key, "bytes=2-5").await;
    assert_eq!(get.status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(&get.body[..], b"2345");
    assert_eq!(get.header(CONTENT_LENGTH).as_deref(), Some("4"));
    assert_eq!(get.header(CONTENT_RANGE).as_deref(), Some("bytes 2-5/10"));
}

#[tokio::test]
async fn matching_if_none_match_returns_304_without_a_body() {
    let app = TestApp::new();
    let bucket = "photos";
    let key = "cached.txt";
    let body = b"cache me";
    app.create_bucket(bucket).await;
    let put = app.put_object(bucket, key, "text/plain", body).await;
    let etag = put.header(ETAG).expect("PUT returns ETag");

    let get = app.get_object_if_none_match(bucket, key, &etag).await;
    assert_eq!(get.status, StatusCode::NOT_MODIFIED);
    assert!(get.body.is_empty());
}

#[tokio::test]
async fn put_without_content_type_defaults_to_octet_stream() {
    let app = TestApp::new();
    let bucket = "blobs";
    let key = "mystery.bin";
    app.create_bucket(bucket).await;

    let req = Request::builder()
        .method("PUT")
        .uri(format!("/{bucket}/{key}"))
        .body(Body::from(b"\x00\x01\x02\x03".to_vec()))
        .unwrap();
    assert_eq!(app.send(req).await.status, StatusCode::OK);

    let get = app.get_object(bucket, key).await;
    assert_eq!(
        get.header(CONTENT_TYPE).as_deref(),
        Some("application/octet-stream"),
        "a typeless upload must read back as the S3 default type"
    );
}

#[tokio::test]
async fn put_overwrite_serves_the_latest_bytes() {
    let app = TestApp::new();
    let bucket = "docs";
    let key = "readme";
    app.create_bucket(bucket).await;

    app.put_object(bucket, key, "text/plain", b"version one")
        .await;
    let second = app
        .put_object(bucket, key, "text/plain", b"version two now")
        .await;
    assert_eq!(second.status, StatusCode::OK);

    let get = app.get_object(bucket, key).await;
    assert_eq!(&get.body[..], b"version two now", "last writer wins");
}

#[tokio::test]
async fn get_missing_key_is_404() {
    let app = TestApp::new();
    let bucket = "photos";
    app.create_bucket(bucket).await;

    let get = app.get_object(bucket, "does-not-exist.jpg").await;
    assert_eq!(get.status, StatusCode::NOT_FOUND);
    assert_eq!(get.json()["error"], "no such key");
}

#[tokio::test]
async fn create_duplicate_bucket_is_409() {
    let app = TestApp::new();
    let bucket = "photos";
    assert_eq!(app.create_bucket(bucket).await.status, StatusCode::OK);

    let again = app.create_bucket(bucket).await;
    assert_eq!(again.status, StatusCode::CONFLICT);
    assert_eq!(again.json()["error"], "bucket already exists");
}

#[tokio::test]
async fn create_bucket_with_illegal_name_is_400() {
    let app = TestApp::new();
    let bucket = "MyBucket";
    let resp = app.create_bucket(bucket).await;
    assert_eq!(resp.status, StatusCode::BAD_REQUEST);
    assert!(
        resp.json()["error"]
            .as_str()
            .unwrap()
            .contains("bucket name"),
        "the 400 body should explain the naming rule it broke"
    );
}

#[tokio::test]
async fn put_into_illegally_named_bucket_is_400() {
    let app = TestApp::new();
    let resp = app
        .put_object("Bad_Bucket", "k.txt", "text/plain", b"hi")
        .await;
    assert_eq!(resp.status, StatusCode::BAD_REQUEST);
}

// ── DELETE (V3 pointer drop, idempotent) ──────────────────────────────────────

#[tokio::test]
async fn delete_removes_key_and_is_idempotent() {
    let bucket = "photos";
    let key = "beach.jpg";
    let app = TestApp::new();
    app.create_bucket(bucket).await;
    app.put_object(bucket, key, "image/jpeg", b"sunny day")
        .await;

    let del = app.delete_object(bucket, key).await;
    assert_eq!(del.status, StatusCode::NO_CONTENT);

    assert_eq!(
        app.get_object(bucket, key).await.status,
        StatusCode::NOT_FOUND
    );

    assert_eq!(
        app.delete_object(bucket, key).await.status,
        StatusCode::NO_CONTENT
    );
}

#[tokio::test]
async fn oversize_put_is_rejected_413() {
    let bucket = "small";
    let key = "big.bin";
    let app = TestApp::with_max_size(16); // 16-byte ceiling
    app.create_bucket(bucket).await;

    let too_big = vec![b'x'; 64];
    let resp = app
        .put_object(bucket, key, "application/octet-stream", &too_big)
        .await;
    assert_eq!(resp.status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(resp.json()["error"], "entity too large");

    assert_eq!(
        app.committed_blob_count(),
        0,
        "over-cap upload leaves no blob"
    );
    assert_eq!(
        app.get_object(bucket, key).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn put_at_exactly_the_cap_succeeds() {
    let bucket = "small";
    let key = "exact.bin";
    const CAP: u64 = 16;
    let app = TestApp::with_max_size(CAP);
    app.create_bucket(bucket).await;

    let exact = vec![b'y'; CAP as usize];
    let resp = app
        .put_object(bucket, key, "application/octet-stream", &exact)
        .await;
    assert_eq!(resp.status, StatusCode::OK, "a body at the cap is allowed");
    assert_eq!(&app.get_object(bucket, key).await.body[..], &exact[..]);
}

#[tokio::test]
async fn two_keys_with_identical_bytes_share_one_blob() {
    let bucket = "photos";
    let original = "original.jpg";
    let copy = "copy.jpg";
    let app = TestApp::new();
    app.create_bucket(bucket).await;

    let bytes = b"identical image content";
    let a = app.put_object(bucket, original, "image/jpeg", bytes).await;
    let b = app.put_object(bucket, copy, "image/jpeg", bytes).await;

    assert_eq!(
        a.header(ETAG),
        b.header(ETAG),
        "identical bytes → identical ETag"
    );

    assert_eq!(&app.get_object(bucket, original).await.body[..], bytes);
    assert_eq!(&app.get_object(bucket, copy).await.body[..], bytes);
    assert_eq!(
        app.committed_blob_count(),
        1,
        "two keys of identical content must dedup to a single blob on disk"
    );
}

fn object_keys(resp: &Resp) -> Vec<String> {
    resp.json()["objects"]
        .as_array()
        .expect("objects is an array")
        .iter()
        .map(|o| o["key"].as_str().unwrap().to_string())
        .collect()
}

fn common_prefixes(resp: &Resp) -> Vec<String> {
    resp.json()["commonPrefixes"]
        .as_array()
        .expect("commonPrefixes is an array")
        .iter()
        .map(|p| p.as_str().unwrap().to_string())
        .collect()
}

async fn seed(app: &TestApp, bucket: &str, keys: &[&str]) {
    app.create_bucket(bucket).await;
    for key in keys {
        app.put_object(bucket, key, "text/plain", key.as_bytes())
            .await;
    }
}

#[tokio::test]
async fn list_without_delimiter_filters_by_prefix() {
    let bucket = "photos";
    let keys = ["a/b/1.txt", "a/b/2.txt", "a/c.txt", "d.txt"];
    let app = TestApp::new();
    seed(&app, bucket, &keys).await;

    let resp = app.list(bucket, "prefix=a/").await;
    assert_eq!(resp.status, StatusCode::OK);
    assert_eq!(
        object_keys(&resp),
        ["a/b/1.txt", "a/b/2.txt", "a/c.txt"],
        "all keys under a/, sorted; d.txt excluded"
    );
    assert!(
        common_prefixes(&resp).is_empty(),
        "no delimiter → no folders"
    );
}

#[tokio::test]
async fn list_with_delimiter_rolls_up_common_prefixes() {
    let bucket = "photos";
    let app = TestApp::new();
    let keys = ["a/b/1.txt", "a/b/2.txt", "a/c.txt", "d.txt"];
    seed(&app, bucket, &keys).await;

    let resp = app.list(bucket, "prefix=a/&delimiter=/").await;
    assert_eq!(
        object_keys(&resp),
        ["a/c.txt"],
        "only the leaf key under a/ is listed"
    );
    assert_eq!(
        common_prefixes(&resp),
        ["a/b/"],
        "a/b/1 and a/b/2 collapse into the single common prefix a/b/"
    );
}

#[tokio::test]
async fn list_paginates_with_max_keys_and_continuation_token() {
    let app = TestApp::new();
    let keys = ["k0", "k1", "k2", "k3", "k4", "k5", "k6"];
    seed(&app, "photos", &keys).await;

    let mut seen = Vec::new();
    let mut token: Option<String> = None;
    loop {
        let query = match &token {
            Some(t) => format!("max-keys=3&continuation-token={t}"),
            None => "max-keys=3".to_string(),
        };
        let resp = app.list("photos", &query).await;
        let page = object_keys(&resp);
        assert!(page.len() <= 3, "no page may exceed max-keys");
        seen.extend(page);

        match resp.json()["nextContinuationToken"].as_str() {
            Some(t) => token = Some(t.to_string()),
            None => {
                assert_eq!(
                    resp.json()["isTruncated"],
                    false,
                    "last page is not truncated"
                );
                break;
            }
        }
    }

    let mut expected: Vec<String> = keys.iter().map(|s| s.to_string()).collect();
    expected.sort();
    assert_eq!(
        seen, expected,
        "pagination visits every key exactly once, in sorted order"
    );
}

#[tokio::test]
async fn list_empty_bucket_returns_no_objects() {
    let bucket = "empty";
    let app = TestApp::new();
    app.create_bucket(bucket).await;

    let resp = app.list(bucket, "").await;
    assert_eq!(resp.status, StatusCode::OK);
    assert!(
        object_keys(&resp).is_empty(),
        "a fresh bucket lists no objects"
    );
    assert_eq!(resp.json()["isTruncated"], false);
}

#[tokio::test]
async fn key_with_slashes_is_stored_flat_and_round_trips() {
    let bucket = "photos";
    let app = TestApp::new();
    app.create_bucket(bucket).await;

    let key = "2024/summer/trip/DSC_0001.jpg";
    app.put_object(bucket, key, "image/jpeg", b"a photo").await;

    assert_eq!(&app.get_object(bucket, key).await.body[..], b"a photo");
    assert_eq!(app.committed_blob_count(), 1);
}
