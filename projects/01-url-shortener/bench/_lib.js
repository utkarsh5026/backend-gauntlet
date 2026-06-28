// Shared helpers for the bench orchestration scripts.
// Plain Node, zero dependencies — run any script with `node bench/<file>.js`.
//
// (redirect.js is the exception: it's run by *k6*, not Node, so it uses k6's
//  own module system. Everything else here is ordinary Node.)

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, ".."); // projects/01-url-shortener
const COMPOSE = path.join(ROOT, "docker-compose.yml");

// Load ../.env into process.env. Does NOT override variables already set in the
// real environment (so `N=50000 node bench/run.js` still wins over the file).
function loadEnv() {
  const file = path.join(ROOT, ".env");
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue; // skip blank lines and # comments
    let val = m[2].replace(/\s+#.*$/, "").trim(); // strip trailing "  # comment"
    val = val.replace(/^["']|["']$/g, ""); // strip surrounding quotes
    if (process.env[m[1]] === undefined) process.env[m[1]] = val;
  }
}

// Is a command on PATH?
function has(cmd) {
  return spawnSync("sh", ["-c", `command -v ${cmd}`], { stdio: "ignore" }).status === 0;
}

// Run a command, inheriting stdio (live output). Throws if it fails.
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(`${cmd} exited with ${r.status ?? r.signal}`);
  return r;
}

// Run a command and return its stdout as a string. Throws if it fails.
function capture(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(`${cmd} exited ${r.status}: ${r.stderr || ""}`);
  return r.stdout;
}

function requireEnv(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`missing ${name} — set it or create ${path.join(ROOT, ".env")}`);
    process.exit(1);
  }
  return v;
}

// Pick the psql invocation: local psql if present, else inside the compose
// 'postgres' container (so Docker alone is enough).
function psqlArgv(extra) {
  if (has("psql")) return ["psql", [requireEnv("DATABASE_URL"), ...extra]];
  return [
    "docker",
    ["compose", "-f", COMPOSE, "exec", "-T", "postgres", "psql", "-U", "shortener", "-d", "shortener", ...extra],
  ];
}

// Execute a SQL script (fed on stdin).
function psqlExec(sql) {
  if (!has("psql")) console.error("(no local psql — running inside the compose 'postgres' container)");
  const [cmd, args] = psqlArgv(["-v", "ON_ERROR_STOP=1", "-q"]);
  run(cmd, args, { input: sql, stdio: ["pipe", "inherit", "inherit"] });
}

// Run a single query and return the bare scalar/rows as text.
function psqlQuery(sql) {
  const [cmd, args] = psqlArgv(["-tAc", sql]);
  return capture(cmd, args).trim();
}

module.exports = { ROOT, COMPOSE, loadEnv, has, run, capture, requireEnv, psqlExec, psqlQuery };
