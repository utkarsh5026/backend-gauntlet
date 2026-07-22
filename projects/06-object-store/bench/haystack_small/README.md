# bench/haystack_small — FileCas vs Haystack for tiny objects

Measures the **small-object packing** tradeoff in this store:

| Layout | On disk | Commit path |
|--------|---------|-------------|
| FileCas | one file under `objects/ab/cd/<digest>` | temp → fsync → rename per blob |
| Haystack | needles in a few `volumes/*.dat` + `needles.json` | append + fsync volume, rewrite idx |

Same unique payloads, two write policies. No HTTP — drives
[`Store::commit_bytes`](../../src/store/mod.rs) / [`Store::open_blob`](../../src/store/mod.rs)
so the delta is physical layout (inodes / append / ranged read), not the router.

This is a **From the field** measurement (Haystack packing), not the graded
Definition-of-done boss fight. Numbers still belong in `docs/06-benchmarks.md`.

## Why in-process (not k6)

1. Layout is a `Store` concern; S3 keys would only add index noise.
2. Unique counter-stamped payloads force real CAS commits (no dedup short-circuit).
3. Footprint (`objects/` file count vs `volumes/*.dat` count) is observable
   without a network client.

## Quick start

```bash
# from projects/06-object-store/
make bench-haystack

# or directly:
cargo run --release -p object-store --features bench-tools --bin haystack_small

# knobs (env):
COUNT=10000 SIZE=4K WARMUP=100 cargo run --release -p object-store \
  --features bench-tools --bin haystack_small
```

Always `--release`. Debug builds measure the wrong thing.

Raw JSON lands in `bench/results/` (gitignored). Curate the table you care
about into `docs/06-benchmarks.md`.

## Defaults

| Knob | Default | Meaning |
|------|---------|---------|
| `COUNT` | `10000` | unique objects committed (timed) |
| `SIZE` | `4K` | payload bytes (`256`, `1K`, `4K`, `16K`, …) |
| `WARMUP` | `100` | commits before the timed write phase |
| `DROP_CACHES` | `0` | set `1` to attempt `sudo` drop between phases/layouts |

## Phases (what the binary does)

```
for layout in {file_cas, haystack}:
  open Store  →  warmup commits  →  timed N commits
              →  footprint walk  →  timed shuffled opens+reads
→ print table  →  write bench/results/haystack_small-*.json
```

## Honest-measurement checklist

1. **Page cache.** Second-pass reads are often RAM. For disk-bound numbers use
   `DROP_CACHES=1` (passwordless sudo) or drop caches yourself between layouts.
2. **`needles.log` + checkpoint.** Commits append a WAL line (cheap); a
   background task rewrites `needles.json` and truncates the log. Write latency
   should track append+fsync, not full JSON rewrite — unless the WAL grows
   huge before checkpoint.
3. **Volume soft-cap.** Default is 1 MiB (`DEFAULT_MAX_VOLUME_SIZE`). Set
   `HAYSTACK_MAX_VOLUME_SIZE=1073741824` (1 GiB, raw bytes) so thousands of
   4 KiB needles stay in one `.dat`. That `vol_n` column is part of the proof.
4. **Unique payloads.** Dedup would make FileCas look artificially cheap on
   repeat commits; this harness stamps each object so every commit is new.

## What each column answers

| Column | Question |
|--------|----------|
| `w_ops/s` / `w_p50` / `w_p99` | Commit cost (rename storm vs append + idx rewrite) |
| `r_ops/s` / `r_p50` / `r_p99` | Open+read cost (many files vs seek in volumes) |
| `obj_n` / `vol_n` | Inode / file-count pressure after the fill |

Fill in curated results under **Haystack vs FileCas** in `docs/06-benchmarks.md`.
