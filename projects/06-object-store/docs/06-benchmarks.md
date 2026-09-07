# 06 — Benchmarks

Numbers from load / micro benches for this project. Raw artifacts live under
`bench/results/` (gitignored); curate the tables you care about here.

## Definition of done (graded)

Harness: `make bench` → [`bench/harness/`](../bench/harness/README.md). In-process
against the real ASGI app, so these include routing, streaming and the index
write, but not TCP.

### Method

- `SIZE=256M PUTS=8 PART_SIZE=16M CHUNK=256K`, page cache warm
- The **download** is driven against the raw ASGI app, not httpx — see below
- RSS is `ru_maxrss`, a high-water mark, so it can only climb

| Scenario | Metric | Result | Notes |
|----------|--------|--------|-------|
| Sustained upload | MiB/s | 58.2 | 256 MiB streamed in 256 KiB chunks |
| Sustained download | MiB/s | 321.1 | Same object, bounded reads |
| **Flat RSS** | growth over a 256 MiB object | **0.8 MiB (0.29%)** | V2's payoff |
| Dedup | 8 identical PUTs | 1 blob, 87.5% saved | V1's payoff |
| Multipart ETag | matches `md5(concat(part md5s))-N` | ✅ exact | V4's wire compat |
| Crash mid-PUT | all-or-nothing | see `crash.py` | Needs a real process |

### The measurement that lied

The first run of this reported **261 MiB of RSS growth** over a 64 MiB object —
about 4×, which looks exactly like the bug V2 exists to prevent. It was the
test, not the server: httpx's `ASGITransport` accumulates the entire response
body in a list before returning a response, so measuring RSS around an httpx
download measures *the client's* buffer.

Driving the ASGI app directly with a `send` that counts and discards gives the
0.29% above. The lesson generalises past this project: a memory measurement is
only about the code you think it is when nothing between you and it buffers.

### Where the ceiling is (why, not just what)

Upload is roughly 5× slower than download, and that asymmetry is the finding.
The PUT path does three things per chunk that the GET path does not: two hash
updates (SHA-256 for the content address, MD5 for the ETag) and a
`asyncio.to_thread` hop for the write. `hashlib` releases the GIL on large
buffers, so the hashing itself parallelises across threads — but the per-chunk
interpreter overhead does not, and at a 256 KiB chunk over 256 MiB that is 1,024
round trips through the thread pool.

The knob to try first is therefore the chunk size, not the hash. **Commit a
`py-spy` flamegraph (`make profile`) and a `memray` run before believing any of
this** — the SPEC's Definition of done asks for the profile precisely because
the paragraph above is a hypothesis until a profile confirms it.

### Haystack vs FileCas (From the field)

Harness: `make bench-haystack` → [`bench/haystack_small/`](../bench/haystack_small/README.md).
`COUNT=800 SIZE=4K`, volume cap 1 GiB, unique payloads so nothing dedups.

| layout | w ops/s | w p50 | w p99 | r ops/s | r p50 | r p99 | files |
|---|---:|---:|---:|---:|---:|---:|---:|
| file_cas | 539 | 1.76 ms | 3.20 ms | 4,532 | 0.21 ms | 0.35 ms | 900 objects |
| haystack | 617 | 1.58 ms | 2.54 ms | 5,360 | 0.18 ms | 0.28 ms | 1 volume |

Packing is ~14% faster to write and ~18% faster to read at this size, but the
number that matters is the last column: 900 inodes against 1. The latency win is
modest because 800 objects is nowhere near where a flat directory hurts; the
inode win is what compounds, and it is why the technique exists.

## Hot vs cold tier (From the field)

Transparent lifecycle tiering: hot `objects/<digest>` vs cold `cold/<ab>/<cd>/<digest>.zst`.

> **Record which codec ran.** The cold tier prefers CPython 3.14's
> `compression.zstd` and falls back to stdlib gzip when it is absent, so a
> compression ratio is not comparable across interpreters.
Harness: `make bench-tier` → [`bench/hot_vs_cold/`](../bench/hot_vs_cold/README.md).

### Method

- `uv run python bench/hot_vs_cold/main.py`
- In-process store + `Lifecycle.run_once_at` with an injected clock (no waiting)
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