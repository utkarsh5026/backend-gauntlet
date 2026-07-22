//! FileCas vs Haystack — small-object packing microbench.
//!
//! In-process: temp data dir + [`Store`] write/read only (no HTTP). Measures
//! commit + open latency/throughput and on-disk file count for thousands of
//! tiny unique blobs. See `bench/haystack_small/README.md`.
//!
//! ```text
//! cargo run --release -p object-store --features bench-tools --bin haystack_small
//! ```

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use chrono::Utc;
use object_store::object::Digest;
use object_store::store::{BlobLayoutKind, Store};
use serde::Serialize;
use tempfile::TempDir;
use tokio::io::AsyncReadExt;

struct Config {
    count: u32,
    size: u64,
    warmup: u32,
    drop_caches: bool,
}

impl Config {
    fn from_env() -> Self {
        Self {
            count: env_u32("COUNT", 10_000),
            size: parse_size_token(&std::env::var("SIZE").unwrap_or_else(|_| "4K".into()))
                .unwrap_or_else(|| {
                    eprintln!("bad SIZE — use e.g. 256, 1K, 4K, 16K");
                    std::process::exit(2);
                }),
            warmup: env_u32("WARMUP", 100),
            drop_caches: std::env::var("DROP_CACHES").ok().as_deref() == Some("1"),
        }
    }
}

fn env_u32(name: &str, default: u32) -> u32 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
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

/// Unique payload so CAS dedup cannot short-circuit FileCas.
fn unique_payload(size: usize, index: u32) -> Vec<u8> {
    let mut out = vec![0u8; size];
    let mut state = 0xDEAD_BEEF_u64 ^ (index as u64) ^ ((size as u64) << 17);
    for b in &mut out {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
        *b = (state >> 33) as u8;
    }
    // Guarantee uniqueness even if LCG collided somehow: stamp the index.
    let stamp = index.to_le_bytes();
    let n = stamp.len().min(out.len());
    out[..n].copy_from_slice(&stamp[..n]);
    out
}

fn shuffle_indices(n: usize, seed: u64) -> Vec<usize> {
    let mut idx: Vec<usize> = (0..n).collect();
    let mut state = seed;
    for i in (1..n).rev() {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
        let j = (state as usize) % (i + 1);
        idx.swap(i, j);
    }
    idx
}

fn percentile_ms(sorted: &[Duration], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((p * (sorted.len() as f64 - 1.0)).round() as usize).min(sorted.len() - 1);
    sorted[idx].as_secs_f64() * 1000.0
}

fn maybe_drop_caches(enabled: bool) {
    if !enabled {
        return;
    }
    match std::process::Command::new("sudo")
        .args(["-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"])
        .status()
    {
        Ok(s) if s.success() => eprintln!("  dropped page caches"),
        Ok(s) => eprintln!("  drop_caches exited {s} — continuing with warm cache"),
        Err(e) => eprintln!("  drop_caches failed ({e}) — continuing with warm cache"),
    }
}

fn human_bytes(n: u64) -> String {
    const K: f64 = 1024.0;
    let n = n as f64;
    if n < K {
        format!("{n:.0} B")
    } else if n < K * K {
        format!("{:.1} KiB", n / K)
    } else if n < K * K * K {
        format!("{:.2} MiB", n / (K * K))
    } else {
        format!("{:.2} GiB", n / (K * K * K))
    }
}

#[derive(Debug, Serialize)]
struct Footprint {
    objects_files: u64,
    volume_files: u64,
    objects_bytes: u64,
    volume_bytes: u64,
    index_bytes: u64,
}

fn count_files_and_bytes(dir: &Path) -> (u64, u64) {
    let mut files = 0u64;
    let mut bytes = 0u64;
    let Ok(entries) = std::fs::read_dir(dir) else {
        return (0, 0);
    };
    let mut stack: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
    while let Some(path) = stack.pop() {
        let Ok(meta) = path.metadata() else {
            continue;
        };
        if meta.is_dir() {
            if let Ok(rd) = std::fs::read_dir(&path) {
                for e in rd.flatten() {
                    stack.push(e.path());
                }
            }
        } else if meta.is_file() {
            files += 1;
            bytes += meta.len();
        }
    }
    (files, bytes)
}

fn measure_footprint(root: &Path) -> Footprint {
    let objects = root.join("objects");
    let volumes = root.join("volumes");
    let (objects_files, objects_bytes) = count_files_and_bytes(&objects);
    let mut volume_files = 0u64;
    let mut volume_bytes = 0u64;
    let mut index_bytes = 0u64;
    if let Ok(rd) = std::fs::read_dir(&volumes) {
        for e in rd.flatten() {
            let path = e.path();
            let Ok(meta) = path.metadata() else {
                continue;
            };
            if !meta.is_file() {
                continue;
            }
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name.ends_with(".dat") {
                volume_files += 1;
                volume_bytes += meta.len();
            } else if name == "needles.json" {
                index_bytes = meta.len();
            }
        }
    }
    Footprint {
        objects_files,
        volume_files,
        objects_bytes,
        volume_bytes,
        index_bytes,
    }
}

#[derive(Serialize)]
struct PhaseStats {
    samples: u32,
    wall_secs: f64,
    ops_per_sec: f64,
    latency_p50_ms: f64,
    latency_p99_ms: f64,
}

#[derive(Serialize)]
struct LayoutReport {
    layout: String,
    write: PhaseStats,
    read: PhaseStats,
    footprint: Footprint,
}

#[derive(Serialize)]
struct Report {
    created_at: String,
    count: u32,
    size_bytes: u64,
    warmup: u32,
    drop_caches: bool,
    layouts: Vec<LayoutReport>,
}

fn summarize_phase(samples: &[Duration], wall: Duration) -> PhaseStats {
    let mut sorted = samples.to_vec();
    sorted.sort();
    let n = samples.len() as f64;
    let wall_secs = wall.as_secs_f64();
    PhaseStats {
        samples: samples.len() as u32,
        wall_secs,
        ops_per_sec: if wall_secs > 0.0 { n / wall_secs } else { 0.0 },
        latency_p50_ms: percentile_ms(&sorted, 0.50),
        latency_p99_ms: percentile_ms(&sorted, 0.99),
    }
}

async fn run_layout(kind: BlobLayoutKind, cfg: &Config) -> LayoutReport {
    let dir = TempDir::new().expect("temp data dir");
    let store = Store::open_with_layout(dir.path(), kind).expect("open store");
    let size = cfg.size as usize;

    eprintln!(
        "→ layout={}  count={}  size={}",
        kind.as_str(),
        cfg.count,
        human_bytes(cfg.size)
    );

    // Warmup commits (not timed).
    for i in 0..cfg.warmup {
        let payload = unique_payload(size, u32::MAX - i);
        store.commit_bytes(&payload).await.expect("warmup commit");
    }

    maybe_drop_caches(cfg.drop_caches);

    let mut digests: Vec<Digest> = Vec::with_capacity(cfg.count as usize);
    let mut write_samples = Vec::with_capacity(cfg.count as usize);
    let write_wall = Instant::now();
    for i in 0..cfg.count {
        let payload = unique_payload(size, i);
        let t0 = Instant::now();
        let digest = store.commit_bytes(&payload).await.expect("commit");
        write_samples.push(t0.elapsed());
        digests.push(digest);
    }
    let write_wall = write_wall.elapsed();
    let write = summarize_phase(&write_samples, write_wall);
    eprintln!(
        "  write: {:.1} ops/s  p50={:.3}ms  p99={:.3}ms  wall={:.2}s",
        write.ops_per_sec, write.latency_p50_ms, write.latency_p99_ms, write.wall_secs
    );

    let footprint = measure_footprint(dir.path());
    eprintln!(
        "  footprint: objects_files={}  volume_files={}  objects={}  volumes={}  needles.json={}",
        footprint.objects_files,
        footprint.volume_files,
        human_bytes(footprint.objects_bytes),
        human_bytes(footprint.volume_bytes),
        human_bytes(footprint.index_bytes),
    );

    maybe_drop_caches(cfg.drop_caches);

    let order = shuffle_indices(digests.len(), 0xC0FFEE ^ cfg.count as u64);
    let mut read_samples = Vec::with_capacity(digests.len());
    let mut buf = Vec::with_capacity(size);
    let read_wall = Instant::now();
    for &i in &order {
        let digest = &digests[i];
        let t0 = Instant::now();
        let mut reader = store.open_blob(digest).await.expect("open");
        buf.clear();
        reader.read_to_end(&mut buf).await.expect("read");
        read_samples.push(t0.elapsed());
        assert_eq!(
            buf.len(),
            size,
            "byte length for digest {}",
            digest.as_str()
        );
    }
    let read_wall = read_wall.elapsed();
    let read = summarize_phase(&read_samples, read_wall);
    eprintln!(
        "  read:  {:.1} ops/s  p50={:.3}ms  p99={:.3}ms  wall={:.2}s",
        read.ops_per_sec, read.latency_p50_ms, read.latency_p99_ms, read.wall_secs
    );

    // Keep store alive until footprint / reads done.
    drop(store);

    LayoutReport {
        layout: kind.as_str().into(),
        write,
        read,
        footprint,
    }
}

fn print_table(cfg: &Config, layouts: &[LayoutReport]) {
    println!();
    println!(
        "haystack_small  COUNT={}  SIZE={}  WARMUP={}  DROP_CACHES={}",
        cfg.count,
        human_bytes(cfg.size),
        cfg.warmup,
        cfg.drop_caches as u8
    );
    println!(
        "{:<10} {:>10} {:>8} {:>8} {:>10} {:>8} {:>8} {:>8} {:>8}",
        "layout", "w_ops/s", "w_p50", "w_p99", "r_ops/s", "r_p50", "r_p99", "obj_n", "vol_n"
    );
    println!("{}", "-".repeat(92));
    for l in layouts {
        println!(
            "{:<10} {:>10.1} {:>7.3}ms {:>7.3}ms {:>10.1} {:>7.3}ms {:>7.3}ms {:>8} {:>8}",
            l.layout,
            l.write.ops_per_sec,
            l.write.latency_p50_ms,
            l.write.latency_p99_ms,
            l.read.ops_per_sec,
            l.read.latency_p50_ms,
            l.read.latency_p99_ms,
            l.footprint.objects_files,
            l.footprint.volume_files,
        );
    }
    println!();
}

fn write_report(report: &Report) -> PathBuf {
    let out_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("bench/results");
    std::fs::create_dir_all(&out_dir).expect("mkdir results");
    let stamp = Utc::now().format("%Y%m%d-%H%M%S");
    let path = out_dir.join(format!("haystack_small-{stamp}.json"));
    let json = serde_json::to_vec_pretty(report).expect("serialize");
    std::fs::write(&path, json).expect("write report");
    path
}

#[tokio::main]
async fn main() {
    let cfg = Config::from_env();
    // Same rule as Haystack::open — banner only; open() is the source of truth.
    let haystack_max = std::env::var("HAYSTACK_MAX_VOLUME_SIZE")
        .ok()
        .and_then(|raw| raw.trim().parse().ok())
        .filter(|&n| n > 0)
        .unwrap_or(object_store::store::haystack::DEFAULT_MAX_VOLUME_SIZE);
    eprintln!(
        "haystack_small: count={} size={} warmup={} drop_caches={} haystack_max_volume={}",
        cfg.count,
        human_bytes(cfg.size),
        cfg.warmup,
        cfg.drop_caches,
        human_bytes(haystack_max),
    );
    eprintln!("(always use --release; see bench/haystack_small/README.md)\n");

    let mut layouts = Vec::new();
    for kind in [BlobLayoutKind::FileCas, BlobLayoutKind::Haystack] {
        layouts.push(run_layout(kind, &cfg).await);
        eprintln!();
    }

    print_table(&cfg, &layouts);
    let report = Report {
        created_at: Utc::now().to_rfc3339(),
        count: cfg.count,
        size_bytes: cfg.size,
        warmup: cfg.warmup,
        drop_caches: cfg.drop_caches,
        layouts,
    };
    let path = write_report(&report);
    eprintln!("wrote {}", path.display());
    eprintln!("curate the table into docs/06-benchmarks.md (Haystack vs FileCas).");
}
