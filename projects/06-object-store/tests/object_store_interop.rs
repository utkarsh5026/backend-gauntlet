//! Black-box interop: Arrow's `object_store` crate against this server.
//!
//! Spins up the real axum router on an ephemeral TCP port (the Arrow client
//! needs a URL, not in-process `oneshot`), then drives put/get/list/multipart
//! through `AmazonS3Builder` with signature skipping — the API is still open
//! (SigV4 is a separate SPEC item).

use std::sync::Arc;
use std::time::Duration;

use arrow_object_store::aws::AmazonS3Builder;
use arrow_object_store::path::Path;
use arrow_object_store::{MultipartUpload, ObjectStore, PutPayload};
use bytes::Bytes;
use futures_util::StreamExt;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::sync::oneshot;

struct LiveServer {
    _dir: TempDir,
    endpoint: String,
    _shutdown: oneshot::Sender<()>,
}

impl LiveServer {
    async fn spawn() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let state = AppState::open(dir.path(), DEFAULT_MAX_OBJECT_SIZE).expect("open store");
        let app = routes::router(state);

        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind ephemeral port");
        let addr = listener.local_addr().expect("local addr");
        let (tx, rx) = oneshot::channel::<()>();

        tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async {
                    let _ = rx.await;
                })
                .await
                .expect("serve");
        });

        // Wait until /healthz answers so the Arrow client does not race startup.
        let endpoint = format!("http://{addr}");
        wait_healthy(&endpoint).await;

        Self {
            _dir: dir,
            endpoint,
            _shutdown: tx,
        }
    }

    fn client(&self, bucket: &str) -> Arc<dyn ObjectStore> {
        let s3 = AmazonS3Builder::new()
            .with_endpoint(&self.endpoint)
            .with_bucket_name(bucket)
            .with_region("us-east-1")
            .with_allow_http(true)
            .with_virtual_hosted_style_request(false)
            .with_skip_signature(true)
            .build()
            .expect("build AmazonS3 client");
        Arc::new(s3)
    }

    async fn create_bucket(&self, bucket: &str) {
        let url = format!("{}/{bucket}", self.endpoint);
        let res = reqwest::Client::new()
            .put(&url)
            .send()
            .await
            .expect("create bucket request");
        assert!(
            res.status().is_success(),
            "CreateBucket failed: {}",
            res.status()
        );
    }
}

async fn wait_healthy(endpoint: &str) {
    let client = reqwest::Client::new();
    let url = format!("{endpoint}/healthz");
    for _ in 0..50 {
        if let Ok(res) = client.get(&url).send().await {
            if res.status().is_success() {
                return;
            }
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("server at {endpoint} never became healthy");
}

#[tokio::test]
async fn arrow_object_store_put_get_list_round_trip() {
    let server = LiveServer::spawn().await;
    let bucket = "interop";
    server.create_bucket(bucket).await;
    let store = server.client(bucket);

    let path = Path::from("docs/hello.txt");
    let payload = Bytes::from_static(b"hello from arrow object_store");
    store
        .put(&path, PutPayload::from(payload.clone()))
        .await
        .expect("put");

    let got = store.get(&path).await.expect("get");
    let bytes = got.bytes().await.expect("collect get body");
    assert_eq!(bytes, payload);

    let mut listed = Vec::new();
    let mut stream = store.list(Some(&Path::from("docs/")));
    while let Some(meta) = stream.next().await {
        listed.push(meta.expect("list item").location.to_string());
    }
    assert!(
        listed.iter().any(|k| k == "docs/hello.txt"),
        "list under docs/ must include hello.txt, got {listed:?}"
    );
}

#[tokio::test]
async fn arrow_object_store_multipart_round_trip() {
    let server = LiveServer::spawn().await;
    let bucket = "multipart-interop";
    server.create_bucket(bucket).await;
    let store = server.client(bucket);

    let path = Path::from("big/blob.bin");
    // Two parts well above the trivial size so the client engages multipart.
    let part_a = vec![b'a'; 5 * 1024 * 1024];
    let part_b = vec![b'b'; 1024 * 1024];
    let mut expected = part_a.clone();
    expected.extend_from_slice(&part_b);

    let mut upload = store
        .put_multipart(&path)
        .await
        .expect("initiate multipart");

    upload
        .put_part(PutPayload::from(Bytes::from(part_a)))
        .await
        .expect("upload part 1");
    upload
        .put_part(PutPayload::from(Bytes::from(part_b)))
        .await
        .expect("upload part 2");
    upload.complete().await.expect("complete multipart");

    let got = store.get(&path).await.expect("get after multipart");
    let bytes = got.bytes().await.expect("collect body");
    assert_eq!(bytes.len(), expected.len());
    assert_eq!(bytes.as_ref(), expected.as_slice());
}
