#!/usr/bin/env node
//
// Orchestrates the redirect load test.   ->  node bench/run.js
//
//   node bench/run.js                       # defaults: N=10000 RATE=2000 DURATION=30s
//   N=50000 RATE=5000 DURATION=1m node bench/run.js
//   FLUSH=1 node bench/run.js               # flush Redis before each scenario (cold cache)
//
// Each scenario answers a specific question:
//   A-hot-single  one viral slug             -> the pure-cache throughput ceiling
//   B-zipf        realistic skewed traffic   -> the number you actually report
//   D-missing     unknown slugs (404 flood)  -> does negative caching hold the DB?
//
// The cache-OFF baseline (scenario C) is intentionally NOT here yet: the app has
// no cache kill-switch, so "with vs without cache" (SPEC Definition-of-done #2)
// needs a small app change first. See README.md.

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const { ROOT, loadEnv, has, run } = require("./_lib");

loadEnv();

const HERE = __dirname;
const RESULTS = path.join(HERE, "results");
fs.mkdirSync(RESULTS, { recursive: true });

// Resolve config (env wins, then .env, then these defaults) and share it with
// the child processes (seed.js and k6) by writing it back onto process.env.
const cfg = {
  BASE_URL: process.env.BASE_URL || "http://localhost:8080",
  N: process.env.N || "10000",
  RATE: process.env.RATE || "2000",
  DURATION: process.env.DURATION || "30s",
  SKEW: process.env.SKEW || "1.0",
  SLUG_PREFIX: process.env.SLUG_PREFIX || "bench-",
};
Object.assign(process.env, cfg);

async function serverUp() {
  try {
    const r = await fetch(`${cfg.BASE_URL}/healthz`, { signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch {
    return false;
  }
}

function flushCache() {
  if (process.env.FLUSH !== "1") return;
  if (has("redis-cli")) {
    run("redis-cli", ["-u", process.env.REDIS_URL || "redis://localhost:6379/0", "FLUSHDB"], { stdio: "ignore" });
    console.log("(flushed redis — cold cache)");
  } else {
    console.log("(FLUSH=1 but redis-cli missing — skipping flush)");
  }
}

function runScenario(name, dist) {
  console.log(`\n================ scenario ${name} (DIST=${dist}, RATE=${cfg.RATE}, ${cfg.DURATION}) ================`);
  const json = path.join(RESULTS, `${name}.json`);
  // stdio inherited so you watch k6's live progress; the JSON summary is the
  // durable artifact. A non-zero exit is usually a threshold breach — a result,
  // not a script error — so we report it and keep going.
  const r = spawnSync("k6", ["run", "--summary-export", json, path.join(HERE, "redirect.js")], {
    stdio: "inherit",
    env: { ...process.env, DIST: dist },
  });
  if (r.status !== 0) {
    console.log(`(k6 exited ${r.status} — likely a threshold breach; recorded in ${name}.json)`);
  }
}

(async () => {
  if (!has("k6")) {
    console.error("k6 is not installed (single binary): https://k6.io/docs/get-started/installation/");
    console.error("  Ubuntu: add the k6 apt repo then `sudo apt install k6`   ·   macOS: `brew install k6`");
    process.exit(1);
  }

  if (!(await serverUp())) {
    console.error(`server not reachable at ${cfg.BASE_URL}`);
    console.error(`start it:  (cd ${ROOT} && docker compose up -d && cargo run --release -p url-shortener)`);
    process.exit(1);
  }

  run("node", [path.join(HERE, "seed.js")]); // inherits N / SLUG_PREFIX from env

  for (const [name, dist] of [
    ["A-hot-single", "single"],
    ["B-zipf", "zipf"],
    ["D-missing", "missing"],
  ]) {
    flushCache();
    runScenario(name, dist);
  }

  console.log(`\nresults in ${RESULTS}/  (*.json summaries)`);
  console.log("next: copy throughput + p95/p99 from B-zipf into docs/01-benchmarks.md");
  console.log("NOTE: cache-ON only. with-vs-without needs the cache kill-switch — see README.md.");
})().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
