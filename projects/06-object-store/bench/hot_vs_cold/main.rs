//! Hot vs cold tier GET microbench.
//!
//! In-process: temp data dir + real axum router + `Lifecycle::run_once_at`.
//! See `bench/hot_vs_cold/README.md` for methodology.
//!
//! ```text
//! cargo run --release -p object-store --features bench-tools --bin hot_vs_cold
//! ```

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::{Duration as ChronoDuration, Utc};
use http_body_util::BodyExt;
use object_store::index::Index;
use object_store::lifecycle::{Encoding, Lifecycle, LifecyclePolicy, LifecycleRule};
use object_store::object::ObjectRef;
use object_store::{routes, AppState, DEFAULT_MAX_OBJECT_SIZE};
use serde::Serialize;
use tempfile::TempDir;
use tower::ServiceExt;

struct Config {
    sizes: Vec<u64>,
    iters: u32,
    warmup: u32,
    drop_caches: bool,
}

impl Config {
    fn from_env() -> Self {
        let sizes = parse_sizes(std::env::var("SIZES").unwrap_or_else(|_| "1M,4M,16M".into()));
        let iters = env_u32("ITERS", 20);
        let warmup = env_u32("WARMUP", 3);
        let drop_caches = std::env::var("DROP_CACHES").ok().as_deref() == Some("1");
        Self {
            sizes,
            iters,
            warmup,
            drop_caches,
        }
    }
}

fn env_u32(name: &str, default: u32) -> u32 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
}

fn parse_sizes(spec: String) -> Vec<u64> {
    spec.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|tok| {
            parse_size_token(tok).unwrap_or_else(|| {
                eprintln!("bad SIZES token {tok:?} — use e.g. 1M,4M,16M");
                std::process::exit(2);
            })
        })
        .collect()
}

fn parse_size_token(tok: &str) -> Option<u64> {
    let tok = tok.trim().to_uppercase();
    let (num, mult) = if let Some(n) = tok.strip_suffix('K') {
        (n, 1024u64)
    } else if let Some(n) = tok.strip_suffix('M') {
        (n, 1024 * 1024)
    } else if let Some(n) = tok.strip_suffix('G') {
        (n, 1024 * 1024 * 1024)
    } else {
        (tok.as_str(), 1)
    };
    num.parse::<u64>().ok().map(|n| n * mult)
}

// ---------------------------------------------------------------------------
// Payloads
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum PayloadKind {
    /// Repeating ASCII — compresses well under zstd.
    Compressible,
    /// LCG pseudo-random bytes — near incompressible.
    Incompressible,
}

impl PayloadKind {
    fn name(self) -> &'static str {
        match self {
            Self::Compressible => "compressible",
            Self::Incompressible => "incompressible",
        }
    }

    fn bytes(self, size: u64) -> Vec<u8> {
        let n = size as usize;
        match self {
            Self::Compressible => {
                let pattern = b"the quick brown fox jumps over the lazy dog\n";
                let mut out = Vec::with_capacity(n);
                while out.len() < n {
                    let take = (n - out.len()).min(pattern.len());
                    out.extend_from_slice(&pattern[..take]);
                }
                out
            }
            Self::Incompressible => {
                // Deterministic LCG — no extra crate, reproducible across runs.
                let mut out = vec![0u8; n];
                let mut state = 0xC0FFEE_u64 ^ size;
                for b in &mut out {
                    state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                    *b = (state >> 33) as u8;
                }
                out
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Harness (router + same-dir lifecycle engine)
// ---------------------------------------------------------------------------

struct Harness {
    _dir: TempDir,
    router: Router,
    lifecycle: Arc<Lifecycle>,
    index: Arc<Index>,
}

impl Harness {
    fn new() -> Self {
        let dir = TempDir::new().expect("temp data dir");
        let state = AppState::open(dir.path(), DEFAULT_MAX_OBJECT_SIZE).expect("open store stack");
        let lifecycle = state.lifecycle.clone();
        let index = state.index.clone();
        let router = routes::router(state);
        Self {
            _dir: dir,
            router,
            lifecycle,
            index,
        }
    }

    async fn send(&self, req: Request<Body>) -> (StatusCode, bytes::Bytes) {
        let res = self.router.clone().oneshot(req).await.expect("infallible");
        let status = res.status();
        let body = res.into_body().collect().await.expect("body").to_bytes();
        (status, body)
    }

    async fn create_bucket(&self, bucket: &str) {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}"))
            .body(Body::empty())
            .unwrap();
        let (status, _) = self.send(req).await;
        assert_eq!(status, StatusCode::OK, "create bucket {bucket}");
    }

    async fn put_object(&self, bucket: &str, key: &str, body: &[u8]) {
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}/{key}"))
            .header("content-type", "application/octet-stream")
            .body(Body::from(body.to_vec()))
            .unwrap();
        let (status, _) = self.send(req).await;
        assert_eq!(status, StatusCode::OK, "PUT {bucket}/{key}");
    }

    async fn set_tier_policy(&self, bucket: &str, tier_after_days: u32) {
        let policy = LifecyclePolicy {
            rules: vec![LifecycleRule {
                id: "bench-tier".into(),
                enabled: true,
                prefix: None,
                tier_after_days: Some(tier_after_days),
                expire_after_days: None,
                noncurrent_expire_after_days: None,
                abort_multipart_after_days: None,
            }],
        };
        let req = Request::builder()
            .method("PUT")
            .uri(format!("/{bucket}?lifecycle"))
            .body(Body::from(serde_json::to_vec(&policy).unwrap()))
            .unwrap();
        let (status, _) = self.send(req).await;
        assert_eq!(status, StatusCode::OK, "set lifecycle on {bucket}");
    }

    /// Full GET timing: TTFB approximated by oneshot return, body drain included.
    async fn get_timed(&self, bucket: &str, key: &str) -> GetSample {
        let req = Request::builder()
            .method("GET")
            .uri(format!("/{bucket}/{key}"))
            .body(Body::empty())
            .unwrap();
        let t0 = Instant::now();
        let res = self.router.clone().oneshot(req).await.expect("infallible");
        let status = res.status();
        let body = res.into_body().collect().await.expect("body").to_bytes();
        let elapsed = t0.elapsed();
        GetSample {
            status,
            bytes: body.len() as u64,
            elapsed,
        }
    }

    async fn encoding_of(&self, bucket: &str, key: &str) -> Encoding {
        let resolved = self
            .index
            .resolve(bucket, key, ObjectRef::Latest)
            .await
            .expect("resolve");
        self.lifecycle
            .locate(&resolved.digest)
            .await
            .expect("locate")
            .encoding
    }

    async fn on_disk_bytes(&self, bucket: &str, key: &str) -> u64 {
        let resolved = self
            .index
            .resolve(bucket, key, ObjectRef::Latest)
            .await
            .expect("resolve");
        let physical = self
            .lifecycle
            .locate(&resolved.digest)
            .await
            .expect("locate");
        tokio::fs::metadata(&physical.path)
            .await
            .expect("metadata")
            .len()
    }
}

struct GetSample {
    status: StatusCode,
    bytes: u64,
    elapsed: Duration,
}

// ---------------------------------------------------------------------------
// Stats + report
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct TierStats {
    encoding: String,
    on_disk_bytes: u64,
    logical_bytes: u64,
    samples: u32,
    latency_p50_ms: f64,
    latency_p99_ms: f64,
    throughput_mib_s: f64,
}

#[derive(Serialize)]
struct CaseReport {
    payload: String,
    size_bytes: u64,
    hot: TierStats,
    cold: TierStats,
    compression_ratio: f64,
    latency_slowdown: f64,
    throughput_ratio: f64,
}

#[derive(Serialize)]
struct Report {
    created_at: String,
    iters: u32,
    warmup: u32,
    drop_caches: bool,
    cases: Vec<CaseReport>,
}

fn percentile_ms(sorted: &[Duration], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((p * (sorted.len() as f64 - 1.0)).round() as usize).min(sorted.len() - 1);
    sorted[idx].as_secs_f64() * 1000.0
}

fn summarize(encoding: &str, on_disk: u64, logical: u64, samples: &[Duration]) -> TierStats {
    let mut sorted = samples.to_vec();
    sorted.sort();
    let total: Duration = samples.iter().copied().sum();
    let thr = if total.as_secs_f64() > 0.0 {
        (logical as f64 * samples.len() as f64) / total.as_secs_f64() / (1024.0 * 1024.0)
    } else {
        0.0
    };
    TierStats {
        encoding: encoding.into(),
        on_disk_bytes: on_disk,
        logical_bytes: logical,
        samples: samples.len() as u32,
        latency_p50_ms: percentile_ms(&sorted, 0.50),
        latency_p99_ms: percentile_ms(&sorted, 0.99),
        throughput_mib_s: thr,
    }
}

fn encoding_label(enc: &Encoding) -> &'static str {
    match enc {
        Encoding::Raw => "raw",
        Encoding::Zstd => "zstd",
    }
}

fn maybe_drop_caches(enabled: bool) {
    if !enabled {
        return;
    }
    eprintln!("  DROP_CACHES=1 — attempting to drop page cache…");
    let status = std::process::Command::new("sudo")
        .args(["sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"])
        .status();
    match status {
        Ok(s) if s.success() => eprintln!("  page cache dropped"),
        Ok(s) => eprintln!("  drop_caches exited {s} — continuing with warm cache"),
        Err(e) => eprintln!("  drop_caches failed ({e}) — continuing with warm cache"),
    }
}

fn human_bytes(n: u64) -> String {
    const K: f64 = 1024.0;
    let n = n as f64;
    if n >= K * K * K {
        format!("{:.2} GiB", n / (K * K * K))
    } else if n >= K * K {
        format!("{:.2} MiB", n / (K * K))
    } else if n >= K {
        format!("{:.2} KiB", n / K)
    } else {
        format!("{n:.0} B")
    }
}

fn print_table(cases: &[CaseReport]) {
    println!();
    println!(
        "{:<16} {:>8}  {:>10} {:>10} {:>8}  {:>9} {:>9} {:>8}  {:>7} {:>8}",
        "payload",
        "size",
        "hot disk",
        "cold disk",
        "ratio",
        "hot p50",
        "cold p50",
        "slow×",
        "hot MiB/s",
        "cold MiB/s"
    );
    println!("{}", "-".repeat(110));
    for c in cases {
        println!(
            "{:<16} {:>8}  {:>10} {:>10} {:>7.2}×  {:>7.2}ms {:>7.2}ms {:>7.2}×  {:>7.1} {:>8.1}",
            c.payload,
            human_bytes(c.size_bytes),
            human_bytes(c.hot.on_disk_bytes),
            human_bytes(c.cold.on_disk_bytes),
            c.compression_ratio,
            c.hot.latency_p50_ms,
            c.cold.latency_p50_ms,
            c.latency_slowdown,
            c.hot.throughput_mib_s,
            c.cold.throughput_mib_s,
        );
    }
    println!();
}

fn write_report(report: &Report) -> PathBuf {
    let out_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("bench/results");
    std::fs::create_dir_all(&out_dir).expect("mkdir results");
    let stamp = Utc::now().format("%Y%m%d-%H%M%S");
    let path = out_dir.join(format!("hot_vs_cold-{stamp}.json"));
    let json = serde_json::to_vec_pretty(report).expect("serialize");
    std::fs::write(&path, json).expect("write report");
    path
}

// ---------------------------------------------------------------------------
// Measure one (payload, size)
// ---------------------------------------------------------------------------

async fn measure_phase(
    h: &Harness,
    bucket: &str,
    key: &str,
    expected_len: u64,
    warmup: u32,
    iters: u32,
) -> Vec<Duration> {
    for _ in 0..warmup {
        let s = h.get_timed(bucket, key).await;
        assert_eq!(s.status, StatusCode::OK);
        assert_eq!(s.bytes, expected_len);
    }
    let mut samples = Vec::with_capacity(iters as usize);
    for _ in 0..iters {
        let s = h.get_timed(bucket, key).await;
        assert_eq!(s.status, StatusCode::OK, "GET {bucket}/{key}");
        assert_eq!(s.bytes, expected_len, "byte-exact round-trip");
        samples.push(s.elapsed);
    }
    samples
}

async fn run_case(h: &Harness, kind: PayloadKind, size: u64, cfg: &Config) -> CaseReport {
    let bucket = "bench";
    let key = format!("{}/{size}", kind.name());
    let payload = kind.bytes(size);

    eprintln!(
        "→ seed {} / {} ({} bytes)",
        kind.name(),
        human_bytes(size),
        size
    );
    h.put_object(bucket, &key, &payload).await;

    let hot_enc = h.encoding_of(bucket, &key).await;
    assert!(
        matches!(hot_enc, Encoding::Raw),
        "fresh PUT must land on the hot tier"
    );
    let hot_disk = h.on_disk_bytes(bucket, &key).await;

    maybe_drop_caches(cfg.drop_caches);
    eprintln!("  measure HOT…");
    let hot_samples = measure_phase(h, bucket, &key, size, cfg.warmup, cfg.iters).await;
    let hot = summarize(encoding_label(&hot_enc), hot_disk, size, &hot_samples);

    // Tier: policy already set on the bucket; advance simulated clock.
    let report = h
        .lifecycle
        .run_once_at(Utc::now() + ChronoDuration::days(400))
        .await
        .expect("tier sweep");
    assert!(
        report.tiered >= 1,
        "expected at least one blob tiered, got {:?}",
        report.tiered
    );

    let cold_enc = h.encoding_of(bucket, &key).await;
    assert!(
        matches!(cold_enc, Encoding::Zstd),
        "after sweep, object must be on the cold tier"
    );
    let cold_disk = h.on_disk_bytes(bucket, &key).await;

    maybe_drop_caches(cfg.drop_caches);
    eprintln!("  measure COLD…");
    let cold_samples = measure_phase(h, bucket, &key, size, cfg.warmup, cfg.iters).await;
    let cold = summarize(encoding_label(&cold_enc), cold_disk, size, &cold_samples);

    let compression_ratio = if cold_disk > 0 {
        hot_disk as f64 / cold_disk as f64
    } else {
        0.0
    };
    let latency_slowdown = if hot.latency_p50_ms > 0.0 {
        cold.latency_p50_ms / hot.latency_p50_ms
    } else {
        0.0
    };
    let throughput_ratio = if cold.throughput_mib_s > 0.0 {
        hot.throughput_mib_s / cold.throughput_mib_s
    } else {
        0.0
    };

    CaseReport {
        payload: kind.name().into(),
        size_bytes: size,
        hot,
        cold,
        compression_ratio,
        latency_slowdown,
        throughput_ratio,
    }
}

// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    let cfg = Config::from_env();
    eprintln!(
        "hot_vs_cold: sizes={:?} iters={} warmup={} drop_caches={}",
        cfg.sizes
            .iter()
            .map(|s| human_bytes(*s))
            .collect::<Vec<_>>(),
        cfg.iters,
        cfg.warmup,
        cfg.drop_caches
    );
    eprintln!("(always use --release; see bench/hot_vs_cold/README.md)\n");

    let h = Harness::new();

    h.create_bucket("bench").await;
    h.set_tier_policy("bench", 30).await;

    let kinds = [PayloadKind::Compressible, PayloadKind::Incompressible];
    let mut cases = Vec::new();
    for kind in kinds {
        for &size in &cfg.sizes {
            cases.push(run_case(&h, kind, size, &cfg).await);
        }
    }

    print_table(&cases);

    let report = Report {
        created_at: Utc::now().to_rfc3339(),
        iters: cfg.iters,
        warmup: cfg.warmup,
        drop_caches: cfg.drop_caches,
        cases,
    };
    let path = write_report(&report);
    eprintln!("wrote {}", path.display());
    eprintln!("curate the table into docs/06-benchmarks.md (Hot vs cold tier).");
}
