# bench/hot_vs_cold — hot vs cold tier GET cost

Measures the **transparent tiering** tradeoff in this store:

| Tier | On disk | GET path |
|------|---------|----------|
| Hot | `objects/<digest>` (raw) | `open_blob` / ranged read |
| Cold | `cold/<digest>.zst` (zstd) | `open_tiered` → streaming decode |

Same keys, same plaintext bytes, two encodings. The client cannot tell which
tier served them — you measure latency, throughput, and bytes-on-disk to see
what you paid for the compression savings.

This is a **From the field** measurement (lifecycle / compressed cold tier),
not the graded Definition-of-done boss fight. Numbers still belong in
`docs/06-benchmarks.md` next to the other benches.

## Why in-process (not k6 against a running server)

Tiering is driven by `Lifecycle::run_once_at(simulated_now)` — a daemon seam,
not an HTTP verb. Running the router + sweeper over one temp data dir (same
trick as `tests/lifecycle_acceptance.rs`) means:

1. No waiting real days for `tier_after_days`.
2. No race with a background sweeper.
3. You can assert `Encoding::Zstd` via `locate` before measuring cold GETs.

Network overhead is deliberately out of the comparison so the delta is
**decode cost + cold I/O shape**, not TCP.

## Quick start

```bash
# from projects/06-object-store/
make bench-tier

# or directly:
cargo run --release -p object-store --features bench-tools --bin hot_vs_cold

# knobs (env):
SIZES=1M,4M,16M ITERS=20 WARMUP=3 cargo run --release -p object-store \
  --features bench-tools --bin hot_vs_cold
```

Always `--release`. Debug builds measure the wrong thing.

Raw JSON lands in `bench/results/` (gitignored). Curate the table you care
about into `docs/06-benchmarks.md`.

## What each scenario answers

| Case | Payload | Question |
|------|---------|----------|
| `compressible` | repeating text | Best-case compression ratio + decode cost |
| `incompressible` | pseudo-random bytes | Worst case: almost no size win, still pay decode |

Sizes default to `1M,4M,16M`. Override with `SIZES=512K,8M`.

## Honest-measurement checklist

1. **Page cache.** A second GET of the same blob is often RAM, not disk. The
   harness prints a note; for headline numbers, drop caches between hot and
   cold phases on Linux (`echo 3 | sudo tee /proc/sys/vm/drop_caches`) or run
   with `DROP_CACHES=1` (requires passwordless sudo — skipped otherwise).
2. **Youngest-referrer rule.** A blob is only cold-eligible when *every*
   referrer's `last_modified` is past `tier_after`. This harness puts one key
   per blob so the rule does not bite.
3. **Label your numbers.** Today's `DOWNLOAD_THROUGHPUT` histogram has no
   `encoding=` label — this bench reports hot/cold in separate columns instead.
4. **Report both axes.** Latency alone hides the win; bytes-on-disk alone
   hides the cost. Always pair them.

## Phases (what the binary does)

```
seed  →  measure HOT gets  →  set policy + run_once_at(future)
      →  assert cold       →  measure COLD gets  →  write JSON + table
```

Fill in curated results under **Hot vs cold tier** in `docs/06-benchmarks.md`.
