# 06 — Benchmarks

Numbers from load / micro benches for this project. Always run with
`--release`. Raw artifacts live under `bench/results/` (gitignored); curate
the tables you care about here.

## Definition of done (graded)

> Placeholder — fill when you run the DoD #2 suite (upload/download throughput,
> flat RSS on a large stream, dedup savings, crash mid-PUT, multipart ETag).

| Scenario | Metric | Result | Notes |
|----------|--------|--------|-------|
| … | … | … | … |

## Hot vs cold tier (From the field)

Transparent lifecycle tiering: hot `objects/<digest>` vs cold `cold/<digest>.zst`.
Harness: `make bench-tier` → [`bench/hot_vs_cold/`](../bench/hot_vs_cold/README.md).

### Method

- Host: WSL2 Linux, `cargo run --release -p object-store --features bench-tools --bin hot_vs_cold`
- In-process axum router + `Lifecycle::run_once_at` (no real-time waiting)
- `SIZES=1M,4M,16M` · `ITERS=20` · `WARMUP=3` · `DROP_CACHES=0` (page cache warm)
- Raw JSON: `bench/results/hot_vs_cold-20260719-071111.json` (2026-07-19)

### Results

| payload | size | hot disk | cold disk | ratio | hot p50 | cold p50 | slow× | hot MiB/s | cold MiB/s |
|---------|------|----------|-----------|------:|--------:|---------:|------:|----------:|-----------:|
| compressible | 1 MiB | 1.00 MiB | 484 B | 2166× | 20.9 ms | 1.2 ms | 0.06× | 48.4 | 827.6 |
| compressible | 4 MiB | 4.00 MiB | 2.06 KiB | 1985× | 80.4 ms | 8.1 ms | 0.10× | 42.1 | 509.6 |
| compressible | 16 MiB | 16.00 MiB | 6.27 KiB | 2611× | 275.1 ms | 13.2 ms | 0.05× | 54.8 | 1186.2 |
| incompressible | 1 MiB | 1.00 MiB | 1.00 MiB | 1.00× | 31.1 ms | 24.6 ms | 0.79× | 32.8 | 41.5 |
| incompressible | 4 MiB | 4.00 MiB | 4.00 MiB | 1.00× | 114.6 ms | 39.8 ms | 0.35× | 33.9 | 102.1 |
| incompressible | 16 MiB | 16.00 MiB | 16.00 MiB | 1.00× | 317.1 ms | 211.1 ms | 0.67× | 50.4 | 71.2 |

`slow×` = cold p50 / hot p50 (&lt; 1 means cold was faster in this run).

### Takeaways

- **Storage win is real on compressible data.** Repeating-text objects shrink
  ~2000–2600× on disk (1 MiB → ~484 B; 16 MiB → ~6 KiB). That is the point of
  the cold tier.
- **Incompressible data does not shrink.** LCG-random payloads stay ~1.00×
  (cold file is slightly *larger* from zstd framing). Tiering those blobs buys
  nothing and still forces a decode path — do not tier blindly.
- **Latency here is not a cold-penalty story.** With a warm page cache and
  hot measured before cold in the same process, cold GETs look *faster*
  (compressible: tiny compressed read + cheap decode; incompressible: still
  &lt;1× slowdown). Re-run with `DROP_CACHES=1` for a disk-bound comparison
  before quoting GET cost in a design doc.
- Transparent round-trip held for every sample (byte-exact plaintext after
  tiering).

## Haystack vs FileCas (small objects)

Small-object packing: one file per digest (`objects/`) vs needles in a handful
of `volumes/*.dat`. Harness: `make bench-haystack` →
[`bench/haystack_small/`](../bench/haystack_small/README.md).

### Method

- Host: WSL2 Linux, `cargo run --release -p object-store --features bench-tools --bin haystack_small`
- In-process [`Store`](../src/store/mod.rs) only (`commit_bytes` / `open_blob`)
- `COUNT=10000` · `SIZE=4K` · `WARMUP=100` · `DROP_CACHES=0` · `HAYSTACK_MAX_VOLUME_SIZE=1073741824`
- Unique payloads (no CAS dedup short-circuit); Haystack index = WAL + background JSON checkpoint
- Raw JSON: `bench/results/haystack_small-20260722-060718.json` (2026-07-22)

### Results

| layout | w ops/s | w p50 | w p99 | r ops/s | r p50 | r p99 | obj files | vol files |
|--------|--------:|------:|------:|--------:|------:|------:|----------:|----------:|
| file_cas | 60.6 | 16.6 ms | 30.5 ms | 2347 | 0.38 ms | 0.90 ms | 10100 | 0 |
| haystack | 63.9 | 13.2 ms | 48.5 ms | 3898 | 0.25 ms | 0.41 ms | 0 | 1 |

### Takeaways

- **Packing wins on footprint.** 10k × 4 KiB → **1 volume** vs ~10k FileCas files.
- **Reads are faster under Haystack** (~1.7× ops/s, lower p50/p99) with a warm
  page cache.
- **Writes are competitive after the WAL** (~64 vs ~61 ops/s; Haystack p50 even
  a bit lower). Background checkpoint keeps `needles.json` from dominating PUT.
- Soft-cap via `HAYSTACK_MAX_VOLUME_SIZE` (raw bytes; see `.env.example`).