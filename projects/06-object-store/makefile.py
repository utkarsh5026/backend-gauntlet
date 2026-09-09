#!/usr/bin/env python3
"""object-store — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, the server, the
Bun/Vite web console, and the probes that make a *store* visible). The
`Makefile` shells out to this file so there is one source of truth with colors,
emojis and readable output. Help tables use `tools/makefile_help.py` (Rich —
auto-installed from `tools/requirements.txt`).

The filesystem *is* the database here: `objects/`, `volumes/`, `index/`, `tmp/`,
`cold/`, `quarantine/` and `uploads/` under `DATA_DIR`. There is no database to
start and no migration to run, which is why this runner has no db bundle — the
compose file exists only for the From-the-field index split, where two of our
own processes share one volume.

The probe tasks are the reason this file is longer than the template. A store
fails in ways a CRUD service does not: "the upload was slow" looks identical
whether the disk is the wall, the hash is, or a blob went to the wrong physical
layout. `make layout` and `make gc` answer those separately.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
    register_smoke_healthz,
)

WEB_DIR = PROJECT_DIR / "web"

# The web console proxies /s3 → the backend and defaults to :9006 (project 06);
# :9000 is skipped because MinIO squats it. Keep backend + proxy on this port.
DEFAULT_PORT = "9006"
DEFAULT_INDEX_PORT = "9106"  # object-store-index (From the field)
WEB_PORT = "5173"  # Vite dev server (see web/vite.config.ts)
COMPOSE_WEB_PORT = "5106"  # nginx console in docker-compose.yml

runner = make_runner(
    crate="object-store",
    help_title="🗄️  object-store",
    project_dir=PROJECT_DIR,
    default_port=DEFAULT_PORT,
    help_footers=[
        (
            "See it in action",
            f"make dev (backend + console; open http://localhost:{WEB_PORT})",
        ),
        (
            "Container stack (index + API + web)",
            f"make stack → console http://localhost:{COMPOSE_WEB_PORT}",
        ),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
register_smoke_healthz(runner)
register_compose_lifecycle(runner)


def backend_port() -> int:
    return int(runner.load_dotenv().get("PORT", DEFAULT_PORT))


def backend_env(port: int) -> dict[str, str]:
    env = runner.load_dotenv()
    env["PORT"] = str(port)
    env.setdefault("DATA_DIR", str(PROJECT_DIR / "data"))
    return env


def data_dir() -> Path:
    return Path(runner.load_dotenv().get("DATA_DIR", str(PROJECT_DIR / "data")))


def get_json(url: str) -> object | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ensure_web_deps() -> None:
    runner.require("bun", "Install Bun: https://bun.sh  (curl -fsSL https://bun.sh/install | bash)")
    if (WEB_DIR / "node_modules").is_dir():
        return
    runner.step("📦", "installing web console deps (bun install)…")
    runner.run(["bun", "install"], cwd=WEB_DIR)


def wait_backend(port: int, proc: subprocess.Popen[bytes], tries: int = 60) -> bool:
    runner.step("⏳", f"waiting for the backend on :{port}…")
    for _ in range(tries):
        if proc.poll() is not None:
            return False
        if runner.port_open("localhost", port):
            return True
        time.sleep(0.5)
    return False


def _popen_session(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    kwargs: dict[str, object] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)  # type: ignore[arg-type]


def spawn_backend(port: int) -> subprocess.Popen[bytes]:
    print(f"{C.DIM}$ PORT={port} uv run {runner.crate}{C.RESET}")
    return _popen_session(["uv", "run", runner.crate], PROJECT_DIR, backend_env(port))


def spawn_web(port: int) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["OBJECT_STORE_URL"] = f"http://localhost:{port}"
    print(f"{C.DIM}$ OBJECT_STORE_URL=http://localhost:{port} bun run dev{C.RESET}")
    return _popen_session(["bun", "run", "dev"], WEB_DIR, env)


def stop_proc(proc: subprocess.Popen[bytes], label: str) -> None:
    if proc.poll() is not None:
        return
    runner.step("🛑", f"stopping {label}…")
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()


@runner.task("web-install", "📦", "Setup", "Install web console deps (bun install)")
def web_install() -> None:
    runner.require("bun", "Install Bun: https://bun.sh")
    runner.step("📦", "installing web console deps…")
    runner.run(["bun", "install"], cwd=WEB_DIR)
    runner.ok("web deps installed")


@runner.task(
    "dev",
    "🚀",
    "Run",
    "Launch backend + web console together (the full demo) — Ctrl-C stops both",
)
def dev() -> None:
    ensure_web_deps()
    port = backend_port()

    backend = spawn_backend(port)
    web: subprocess.Popen[bytes] | None = None
    try:
        if not wait_backend(port, backend):
            runner.fail("backend did not come up (it exited or never bound the port)")
            sys.exit(1)
        runner.ok(f"backend is serving http://localhost:{port}")

        runner.rule(C.MAGENTA)
        print(f"{C.BOLD}{C.MAGENTA}🎬  Object-store console is coming up{C.RESET}")
        print(
            f"   Console:  {C.BOLD}{C.CYAN}http://localhost:{WEB_PORT}{C.RESET} "
            f"{C.DIM}(open this){C.RESET}"
        )
        print(f"   Backend:  {C.DIM}http://localhost:{port}  (S3 path-style API){C.RESET}")
        print(
            f"   {C.DIM}Create a bucket → PUT an object → watch it list. "
            f"Multipart tab shows the -N ETag.{C.RESET}"
        )
        print(f"   {C.DIM}Ctrl-C stops both.{C.RESET}")
        runner.rule(C.MAGENTA)

        web = spawn_web(port)
        try:
            web.wait()
        except KeyboardInterrupt:
            print()
            runner.warn("interrupted — shutting down")
    finally:
        if web is not None:
            stop_proc(web, "web console")
        stop_proc(backend, "backend")


@runner.task("backend", "🐍", "Run", f"Run just the store (PORT={DEFAULT_PORT}, loads .env)")
def backend() -> None:
    port = backend_port()
    runner.step("🐍", f"starting {runner.crate} on http://localhost:{port} …")
    runner.uv("run", runner.crate, env=backend_env(port))


def index_port() -> int:
    return int(runner.load_dotenv().get("INDEX_PORT", DEFAULT_INDEX_PORT))


@runner.task(
    "index-svc",
    "📇",
    "Run",
    f"Run the index microservice only (INDEX_PORT={DEFAULT_INDEX_PORT}; From the field)",
)
def index_svc() -> None:
    """The metadata process — pair with `INDEX_URL=… make backend`."""
    env = runner.load_dotenv()
    env["INDEX_PORT"] = str(index_port())
    env.setdefault("DATA_DIR", str(PROJECT_DIR / "data"))
    runner.step(
        "📇",
        f"starting object-store-index on http://localhost:{index_port()} … "
        f"(internal /v1 API; see docs/05-how-index-as-a-service-works.md)",
    )
    runner.uv("run", "object-store-index", env=env)


@runner.task("frontend", "🌐", "Run", f"Run just the web console (Vite dev, :{WEB_PORT})")
def frontend() -> None:
    ensure_web_deps()
    web_env = dict(os.environ)
    web_env["OBJECT_STORE_URL"] = f"http://localhost:{backend_port()}"
    runner.step(
        "🌐",
        f"starting the console on http://localhost:{WEB_PORT} (proxying /s3 → :{backend_port()})…",
    )
    runner.run(["bun", "run", "dev"], cwd=WEB_DIR, env=web_env, check=False)


@runner.task("web-build", "🏗️", "Run", "Production build of the web console (tsc + vite build)")
def web_build() -> None:
    runner.require("bun", "Install Bun: https://bun.sh")
    ensure_web_deps()
    runner.step("🏗️", "building the web console…")
    runner.run(["bun", "run", "build"], cwd=WEB_DIR)
    runner.ok(f"built → {WEB_DIR / 'dist'}")


@runner.task("up", "🐳", "Services", "Build & start index + object-store + web (compose)")
def up() -> None:
    runner.require("docker", "Install Docker to use the container stack.")
    runner.step("🐳", "docker compose up --build -d …")
    runner.run([*runner.compose, "up", "--build", "-d"], cwd=PROJECT_DIR)
    runner.ok(
        f"stack up — console http://localhost:{COMPOSE_WEB_PORT} "
        f"(API :{DEFAULT_PORT}, index :{DEFAULT_INDEX_PORT})"
    )


@runner.task("deps", "🐳", "Services", "Alias for `up` (full compose stack)")
def deps() -> None:
    up()


@runner.task(
    "stack", "🐳", "Services", f"Alias for `up` — open http://localhost:{COMPOSE_WEB_PORT}"
)
def stack() -> None:
    up()


# --------------------------------------------------------------------------- #
# Probes — make the store's internals visible without a debugger
# --------------------------------------------------------------------------- #


@runner.task("layout", "🗂️", "Probe", "Show what is actually on disk under DATA_DIR")
def layout() -> None:
    """Where the bytes went, and how many.

    The first question when a number surprises you: did the blob land in
    `objects/` or get packed into a `volumes/` needle, is anything sitting in
    `cold/`, and has the scrubber quarantined something. All four look the same
    over HTTP.
    """
    root = data_dir()
    if not root.is_dir():
        runner.warn(f"{root} does not exist yet — PUT something first")
        return

    runner.step("🗂️", f"{root}")
    rows: list[tuple[str, int, int, str]] = []
    for name, note in (
        ("objects", "FileCas blobs, sharded ab/cd/<digest>"),
        ("volumes", "Haystack needles + needles.json/.log"),
        ("cold", "lifecycle-tiered, compressed"),
        ("tmp", "in-flight writes (should be near-empty)"),
        ("uploads", "multipart staging (one dir per session)"),
        ("quarantine", "scrub failures — should be empty"),
        ("index", "(bucket,key) → digest rows"),
    ):
        directory = root / name
        if not directory.is_dir():
            continue
        files = [p for p in directory.rglob("*") if p.is_file()]
        rows.append((name, len(files), sum(p.stat().st_size for p in files), note))

    width = max((len(r[0]) for r in rows), default=0)
    for name, count, size, note in rows:
        flag = C.YELLOW if name in {"quarantine", "tmp"} and count else C.DIM
        print(
            f"  {C.BOLD}{name:<{width}}{C.RESET}  {count:>6} files  "
            f"{size / 1024 / 1024:>9.2f} MiB  {flag}{note}{C.RESET}"
        )

    total = sum(r[2] for r in rows)
    print(f"\n  {C.DIM}total on disk: {total / 1024 / 1024:.2f} MiB{C.RESET}")


@runner.task("dedup", "♻️", "Probe", "Compare logical object bytes against blobs on disk")
def dedup() -> None:
    """The V1 payoff, as one number.

    Sums every live index row's size (what clients think they stored) against
    the blob tree (what the disk actually holds). The gap is dedup, and it is
    the only honest way to show it — a single PUT proves nothing.
    """
    root = data_dir()
    index_root = root / "index"
    if not index_root.is_dir():
        runner.warn("no index yet — PUT something first")
        return

    logical = 0
    keys = 0
    for row in index_root.glob("*/objects/*.json"):
        try:
            meta = json.loads(row.read_text())
        except (ValueError, OSError):
            continue
        latest = meta.get("latest")
        for version in meta.get("versions", []):
            if version.get("id") == latest and version.get("kind", {}).get("type") == "live":
                logical += version["kind"]["size"]
                keys += 1

    physical = sum(
        p.stat().st_size
        for name in ("objects", "volumes", "cold")
        for p in (root / name).rglob("*")
        if p.is_file()
    )

    runner.step("♻️", f"{keys} live keys")
    print(f"  logical (what clients stored):  {logical / 1024 / 1024:>9.2f} MiB")
    print(f"  physical (what the disk holds): {physical / 1024 / 1024:>9.2f} MiB")
    if logical:
        saved = 100 * (1 - physical / logical)
        colour = C.GREEN if saved > 0 else C.DIM
        print(f"  {colour}saved by dedup:                {saved:>9.1f}%{C.RESET}")


@runner.task("gc", "🧹", "Probe", "Report blobs no live key references (does not delete)")
def gc() -> None:
    """Dry-run the mark phase.

    Answers "is that disk usage garbage or data?" without touching anything.
    Reclamation itself is lazy and lives in the running server.
    """
    root = data_dir()
    index_root = root / "index"
    referenced: set[str] = set()
    for row in index_root.glob("*/**/*.json") if index_root.is_dir() else []:
        try:
            meta = json.loads(row.read_text())
        except (ValueError, OSError):
            continue
        for version in meta.get("versions", []):
            kind = version.get("kind", {})
            if kind.get("type") == "live":
                referenced.add(kind["digest"])

    on_disk = {
        p.name: p.stat().st_size
        for p in (root / "objects").rglob("*")
        if p.is_file() and len(p.name) == 64
    }
    orphans = {name: size for name, size in on_disk.items() if name not in referenced}

    runner.step("🧹", f"{len(referenced)} referenced digests, {len(on_disk)} blobs on disk")
    if not orphans:
        runner.ok("no unreferenced blobs")
        return
    total = sum(orphans.values())
    runner.warn(f"{len(orphans)} unreferenced blobs holding {total / 1024 / 1024:.2f} MiB")
    for name in list(orphans)[:10]:
        print(f"  {C.DIM}{name[:16]}…  {orphans[name]:>10} bytes{C.RESET}")
    print(f"  {C.DIM}(the running server's GC reclaims these after its grace window){C.RESET}")


@runner.task("stats", "📊", "Probe", "Scrape /metrics and print the store counters")
def stats() -> None:
    port = backend_port()
    url = f"http://localhost:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        runner.fail(f"could not scrape {url} — is the backend running? (make backend)")
        sys.exit(1)

    runner.step("📊", url)
    interesting = (
        "objects_put_total",
        "objects_get_total",
        "objects_deleted_total",
        "dedup_hits_total",
        "gc_blobs_reclaimed_total",
        "range_requests_served_total",
        "blob_count",
        "total_bytes_stored",
        "in_flight_uploads",
        "multipart_open_sessions",
        "scrub_corruptions_total",
    )
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name = line.split()[0].split("{")[0]
        if any(name.endswith(suffix) for suffix in interesting):
            metric, _, value = line.partition(" ")
            flag = C.YELLOW if "corruptions" in metric and float(value or 0) > 0 else C.RESET
            print(f"  {flag}{metric.replace('object_store_', ''):<42} {value}{C.RESET}")


@runner.task("bench", "📊", "Bench", "Run the boss-fight harness (writes bench/results/)")
def bench() -> None:
    runner.step("📊", "bench/harness/main.py …")
    runner.uv("run", "python", str(PROJECT_DIR / "bench" / "harness" / "main.py"))
    runner.ok("see bench/results/ and docs/06-benchmarks.md")


@runner.task("bench-tier", "📊", "Bench", "Hot vs cold tier GET microbench")
def bench_tier() -> None:
    runner.step("📊", "bench/hot_vs_cold/main.py …")
    runner.uv("run", "python", str(PROJECT_DIR / "bench" / "hot_vs_cold" / "main.py"))
    runner.ok("see bench/results/")


@runner.task("bench-haystack", "📊", "Bench", "FileCas vs Haystack small-object microbench")
def bench_haystack() -> None:
    runner.step("📊", "bench/haystack_small/main.py …")
    runner.uv("run", "python", str(PROJECT_DIR / "bench" / "haystack_small" / "main.py"))
    runner.ok("see bench/results/")


@runner.task("profile", "🔥", "Bench", "py-spy flamegraph of a PUT-heavy run (Definition of done)")
def profile() -> None:
    """Where the seconds actually go.

    The SPEC's Definition of done asks for this, not just a throughput number:
    hashing releases the GIL and per-chunk interpreter overhead does not, so
    which of the two is the wall is a question the flamegraph answers and the
    RPS figure cannot.
    """
    runner.require("py-spy", "Installed as a dev dep — run `make sync` first.")
    out = PROJECT_DIR / "bench" / "results" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", f"py-spy record → {out}")
    runner.uv(
        "run",
        "py-spy",
        "record",
        "--output",
        str(out),
        "--format",
        "flamegraph",
        "--",
        "python",
        str(PROJECT_DIR / "bench" / "harness" / "main.py"),
    )
    runner.ok(f"flamegraph → {out}")


@runner.task("reset", "🗑️", "Run", "Delete DATA_DIR (destroys every stored object)")
def reset() -> None:
    root = data_dir()
    if not root.exists():
        runner.ok(f"{root} is already gone")
        return
    runner.warn(f"removing {root} …")
    shutil.rmtree(root)
    runner.ok("data dir removed")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
