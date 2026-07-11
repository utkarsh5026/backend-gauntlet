//! Subprocess integration tests for graceful shutdown (SIGINT / SIGTERM).
//!
//! These spawn the real `object-store` binary — the same `main.rs` path that
//! handles Ctrl-C and container SIGTERM — then assert the process exits cleanly
//! and does not leak staging files when a slow upload is cut off.

#![cfg(unix)]

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use bytes::Bytes;
use futures_util::stream;
use reqwest::Body;
use tempfile::TempDir;
use tokio::process::{Child, Command};
use tokio::time::{sleep, timeout};

const LOCAL_ADDR: &str = "127.0.0.1:0";

fn bin() -> PathBuf {
    for key in ["CARGO_BIN_EXE_object_store", "CARGO_BIN_EXE_object-store"] {
        if let Ok(path) = std::env::var(key) {
            return PathBuf::from(path);
        }
    }

    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "debug".into());
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("../../target/{profile}/object-store"))
}

fn signal_pid(pid: u32, sig: &str) {
    std::process::Command::new("kill")
        .args([sig, &pid.to_string()])
        .status()
        .expect("kill");
}

fn tmp_count(root: &Path) -> usize {
    std::fs::read_dir(root.join("tmp"))
        .map(|entries| entries.count())
        .unwrap_or(0)
}

struct Server {
    _dir: TempDir,
    root: PathBuf,
    port: u16,
    child: Child,
    client: reqwest::Client,
}

impl Server {
    async fn spawn(extra_env: &[(&str, &str)]) -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let root = dir.path().to_path_buf();
        let port = std::net::TcpListener::bind(LOCAL_ADDR)
            .expect("bind ephemeral port")
            .local_addr()
            .expect("local addr")
            .port();

        let mut cmd = Command::new(bin());
        cmd.env("DATA_DIR", &root)
            .env("PORT", port.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);

        for (key, value) in extra_env {
            cmd.env(key, *value);
        }

        let child = cmd.spawn().expect("spawn object-store binary");
        let client = reqwest::Client::new();
        let server = Self {
            _dir: dir,
            root,
            port,
            child,
            client,
        };
        server.wait_until_ready().await;
        server
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    async fn wait_until_ready(&self) {
        let url = format!("{}/healthz", self.base_url());
        for _ in 0..100 {
            if let Ok(resp) = self.client.get(&url).send().await {
                if resp.status().is_success() && resp.text().await.unwrap_or_default() == "ok" {
                    return;
                }
            }
            sleep(Duration::from_millis(50)).await;
        }
        panic!("server on port {} did not become ready", self.port);
    }

    async fn request_ok(
        &self,
        build: impl FnOnce(&reqwest::Client, String) -> reqwest::RequestBuilder,
        url: String,
        what: &str,
    ) {
        build(&self.client, url)
            .send()
            .await
            .expect(what)
            .error_for_status()
            .expect(what);
    }

    async fn create_bucket(&self, bucket: &str) {
        self.request_ok(
            |client, url| client.put(url),
            format!("{}/{bucket}", self.base_url()),
            "create bucket",
        )
        .await;
    }

    async fn put_bytes(&self, bucket: &str, key: &str, bytes: &[u8]) {
        self.request_ok(
            |client, url| client.put(url).body(bytes.to_vec()),
            format!("{}/{bucket}/{key}", self.base_url()),
            "put object",
        )
        .await;
    }

    async fn get_bytes(&self, bucket: &str, key: &str) -> Bytes {
        let what = "get object";
        self.client
            .get(format!("{}/{bucket}/{key}", self.base_url()))
            .send()
            .await
            .expect(what)
            .error_for_status()
            .expect(what)
            .bytes()
            .await
            .expect(what)
    }

    async fn stop_with_signal(&mut self, sig: &str) -> std::process::ExitStatus {
        let pid = self.child.id().expect("server pid");
        signal_pid(pid, sig);
        timeout(Duration::from_secs(10), self.child.wait())
            .await
            .expect("server did not exit after signal")
            .expect("wait on server child")
    }
}

/// Start a PUT whose body trickles out slowly so we can signal mid-upload.
async fn start_slow_put(server: &Server, bucket: &str, key: &str) -> tokio::task::JoinHandle<()> {
    let url = format!("{}/{bucket}/{key}", server.base_url());
    let client = server.client.clone();
    tokio::spawn(async move {
        let body = Body::wrap_stream(stream::unfold(0u32, |i| async move {
            if i >= 80 {
                return None;
            }
            sleep(Duration::from_millis(100)).await;
            Some((
                Ok::<_, std::convert::Infallible>(Bytes::from(vec![b'x'; 16 * 1024])),
                i + 1,
            ))
        }));
        let _ = client
            .put(url)
            .body(body)
            .timeout(Duration::from_secs(120))
            .send()
            .await;
    })
}

#[tokio::test]
async fn sigint_stops_server_cleanly() {
    let mut server = Server::spawn(&[]).await;
    let status = server.stop_with_signal("-INT").await;
    assert!(status.success(), "SIGINT should exit 0, got {status:?}");
}

#[tokio::test]
async fn sigterm_stops_server_cleanly() {
    let mut server = Server::spawn(&[]).await;
    let status = server.stop_with_signal("-TERM").await;
    assert!(status.success(), "SIGTERM should exit 0, got {status:?}");
}

#[tokio::test]
async fn shutdown_preserves_committed_data() {
    let (bucket, key) = ("photos", "cat.jpg");
    let mut server = Server::spawn(&[]).await;
    let root = server.root.clone();
    server.create_bucket(bucket).await;
    server.put_bytes(bucket, key, b"meow").await;

    let status = server.stop_with_signal("-TERM").await;
    assert!(status.success());

    let server2 = Server::spawn(&[("DATA_DIR", root.to_str().expect("utf8 data dir"))]).await;
    let bytes = server2.get_bytes(bucket, key).await;
    assert_eq!(bytes.as_ref(), b"meow");
}

#[tokio::test]
async fn sigterm_during_slow_put_leaves_no_temp_files() {
    let mut server = Server::spawn(&[("SHUTDOWN_GRACE_SECS", "1")]).await;
    let root = server.root.clone();
    let (bucket, key) = ("photos", "big.bin");
    server.create_bucket(bucket).await;
    let upload = start_slow_put(&server, bucket, key).await;

    sleep(Duration::from_millis(200)).await;
    let status = server.stop_with_signal("-TERM").await;
    assert!(status.success());

    let _ = timeout(Duration::from_secs(5), upload)
        .await
        .expect("slow upload task should finish after server stops");

    assert_eq!(
        tmp_count(&root),
        0,
        "forced shutdown must not leave partial staging files in tmp/"
    );
}
