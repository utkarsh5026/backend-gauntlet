# bench/ — redirect load test

The HTTP load test for the redirect hot path (SPEC *Definition of done #2*).
For the in-process micro-benchmark of the ID generator, see `../benches/id_gen.rs`
(`cargo bench`). Different layer, different tool:

| Layer | Tool | Lives in | Measures |
|-------|------|----------|----------|
| Micro (pure CPU, ns) | criterion | `../benches/` | ID-gen throughput, base62 cost |
| Macro (whole service, under load) | k6 | `./` (here) | redirect req/s + latency tail |

The orchestration scripts are **Node.js** (zero deps — `node bench/<file>.js`).
`redirect.js` is the exception: it's executed by *k6*, not Node, so it uses k6's
own module system.

## Files

- **`seed.js`** — bulk-inserts `N` links straight into Postgres (`bench-00001..`),
  bypassing the rate-limited POST path. Uses local `psql`, or falls back to
  `docker compose exec postgres` so Docker is the only requirement.
- **`redirect.js`** — the k6 scenario. Picks slugs by a configurable
  distribution and hammers `GET /{slug}`.
- **`run.js`** — checks the server is up, seeds, runs all scenarios, saves
  results to `results/`.
- **`smoke.js`** — Node-only sanity check; run this first, it needs no installs
  beyond Node.
- **`_lib.js`** — shared helpers (env loading, running commands, psql). Not an
  entry point.

## Quick start

```bash
# 0. stack up + server running (release build — debug numbers are meaningless)
docker compose up -d
cargo run --release -p url-shortener        # in another terminal

# 1. prove the redirect contract with just Node (no k6 needed yet)
node bench/smoke.js

# 2. install k6 (single binary): https://k6.io/docs/get-started/installation/
#    Ubuntu: sudo apt install k6 (after adding the k6 apt repo) · macOS: brew install k6

# 3. run the load test
node bench/run.js
N=50000 RATE=5000 DURATION=1m node bench/run.js   # heavier
FLUSH=1 node bench/run.js                          # cold cache each scenario
```

`seed.js` can be run on its own too: `N=20 node bench/seed.js`.

## Scenarios (what each one answers)

| Name | `DIST` | Question |
|------|--------|----------|
| `A-hot-single` | `single` | One viral slug — the pure-cache throughput ceiling. |
| `B-zipf` | `zipf` | Realistic power-law traffic — **the number you report**. |
| `D-missing` | `missing` | Unknown-slug flood (404s) — does negative caching hold the DB? |

Tune skew with `SKEW` (default `1.0`; higher = more concentrated on hot keys).

## Three things this harness gets deliberately right

These are the difference between a real benchmark and a misleading one — they're
documented inline in `redirect.js`, but in short:

1. **No redirect-following.** The endpoint returns `308 + Location`. k6 follows
   redirects by default and would benchmark *example.com over the internet*.
   `{ redirects: 0 }` records the 308 and stops. (`smoke.js` shows this live with
   Node's `redirect: "manual"`.)
2. **Open model (arrival rate), not closed (fixed VUs).** `constant-arrival-rate`
   fires `RATE` req/s no matter how slow the server is, so stalls show up in the
   latency tail. A fixed-VU test hides them — *coordinated omission*.
3. **Zipfian keys, not uniform.** Shortener traffic is power-law. Uniform keys
   would tank the hit ratio and make the cache look useless — an artifact, not a
   finding. `DIST=zipf` models reality; `uniform` is there as the contrast.

## Two things this harness does NOT do yet (and why)

These are honest gaps, not bugs — both are blocked on small app changes that the
SPEC's vertical work will add:

- **Cache-OFF baseline (scenario C).** DoD #2 wants throughput *with vs without*
  the cache. That needs a kill-switch in the app (an env flag that forces every
  redirect down the Postgres path). The redirect handler currently always uses
  the cache. Add the flag, then add a `["C-zipf-nocache", "zipf"]` scenario to
  `run.js` (passing `CACHE_DISABLED=1` in its env) and you have the comparison.
- **Server-side cache hit ratio under load.** Also required by DoD #2. That comes
  from a metrics endpoint (the observability checklist — still unchecked). Until
  then you can *infer* cache effectiveness from the latency gap between `B-zipf`
  (warm, skewed) and a `uniform` run, and from the server's per-redirect logs
  (`cache=hit|miss|negative`). The real number waits on the metrics counter.

## Method notes

- Always `--release`. Debug builds measure the wrong thing.
- Keep `N` consistent between seed and run — `run.js` does this for you; if you
  call `redirect.js` directly via `k6 run`, pass the same `N` you seeded.
- Watch *where* the bottleneck is. If Postgres CPU is pinned and the app is idle,
  you're measuring Postgres (that's the point of the cache-off run). If k6's own
  host saturates first, your numbers are about k6, not the service — run the load
  generator off-box for the headline numbers.
- `results/` holds raw output; commit only the curated numbers into
  `docs/01-benchmarks.md`.
