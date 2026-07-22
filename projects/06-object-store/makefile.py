#!/usr/bin/env python3
"""object-store — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(cargo, the Bun/Vite web console, curl smoke checks). The `Makefile` shells out
to this file so you get one source of truth with colors, emojis and readable
output. Help tables use `tools/makefile_help.py` (Rich — auto-installed from
`tools/requirements.txt`).

The filesystem *is* the database (objects/, index/, tmp/, uploads/ under
DATA_DIR). Headline host loop: `make dev` (cargo + Vite). Container-parity
stack: `make stack` — three containers (index ↔ object-store ↔ web) sharing a
volume (docs/05-how-index-as-a-service-works.md).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_cargo_checks,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_setup,
)

WEB_DIR = PROJECT_DIR / "web"

# The web console proxies /s3 → the backend and defaults to :9006 (project 06);
# :9000 is skipped because MinIO squats it. Keep backend + proxy on this port.
DEFAULT_PORT = "9006"
DEFAULT_INDEX_PORT = "9106"  # object-store-index binary (From the field)
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
register_cargo_checks(runner)
register_compose_lifecycle(runner)


def backend_port() -> int:
    return int(runner.load_dotenv().get("PORT", DEFAULT_PORT))


def backend_env(port: int) -> dict[str, str]:
    env = runner.load_dotenv()
    env["PORT"] = str(port)
    env.setdefault("DATA_DIR", str(PROJECT_DIR / "data"))
    return env


def ensure_web_deps() -> None:
    runner.require(
        "bun", "Install Bun: https://bun.sh  (curl -fsSL https://bun.sh/install | bash)"
    )
    if (WEB_DIR / "node_modules").is_dir():
        return
    runner.step("📦", "installing web console deps (bun install)…")
    runner.run(["bun", "install"], cwd=WEB_DIR)


def wait_backend(port: int, proc: subprocess.Popen, tries: int = 60) -> bool:
    runner.step("⏳", f"waiting for the backend on :{port}…")
    for _ in range(tries):
        if proc.poll() is not None:
            return False
        if runner.port_open("localhost", port):
            return True
        time.sleep(0.5)
    return False


def _popen_session(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)


def spawn_backend(port: int) -> subprocess.Popen:
    print(f"{C.DIM}$ PORT={port} cargo run -p {runner.crate}{C.RESET}")
    return _popen_session(
        ["cargo", "run", "-p", runner.crate], PROJECT_DIR, backend_env(port)
    )


def spawn_web(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["OBJECT_STORE_URL"] = f"http://localhost:{port}"
    print(f"{C.DIM}$ OBJECT_STORE_URL=http://localhost:{port} bun run dev{C.RESET}")
    return _popen_session(["bun", "run", "dev"], WEB_DIR, env)


def stop_proc(proc: subprocess.Popen, label: str) -> None:
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
    runner.require("cargo", "Install Rust: https://rustup.rs")
    ensure_web_deps()
    port = backend_port()

    backend = spawn_backend(port)
    web: subprocess.Popen | None = None
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
        print(
            f"   Backend:  {C.DIM}http://localhost:{port}  (S3 path-style API){C.RESET}"
        )
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


@runner.task("backend", "🦀", "Run", f"Run just the Rust store (PORT={DEFAULT_PORT}, loads .env)")
def backend() -> None:
    runner.require("cargo", "Install Rust: https://rustup.rs")
    port = backend_port()
    runner.step("🦀", f"starting {runner.crate} on http://localhost:{port} …")
    runner.run(
        ["cargo", "run", "-p", runner.crate],
        cwd=PROJECT_DIR,
        env=backend_env(port),
    )


def index_port() -> int:
    return int(runner.load_dotenv().get("INDEX_PORT", DEFAULT_INDEX_PORT))


def index_env(port: int) -> dict[str, str]:
    env = runner.load_dotenv()
    env["INDEX_PORT"] = str(port)
    env.setdefault("DATA_DIR", str(PROJECT_DIR / "data"))
    return env


@runner.task(
    "index-svc",
    "📇",
    "Run",
    f"Run index microservice only (INDEX_PORT={DEFAULT_INDEX_PORT}; From the field)",
)
def index_svc() -> None:
    """Host-side metadata process — pair with INDEX_URL=… make backend."""
    runner.require("cargo", "Install Rust: https://rustup.rs")
    port = index_port()
    runner.step(
        "📇",
        f"starting object-store-index on http://localhost:{port} … "
        f"(internal /v1 API; see docs/05-how-index-as-a-service-works.md)",
    )
    runner.run(
        ["cargo", "run", "-p", runner.crate, "--bin", "object-store-index"],
        cwd=PROJECT_DIR,
        env=index_env(port),
    )


@runner.task(
    "up",
    "🐳",
    "Services",
    "Build & start index + object-store + web (docker compose)",
)
def up() -> None:
    runner.require("docker", "Install Docker to use the container stack.")
    runner.step("🐳", "docker compose up --build -d …")
    runner.run(
        [*runner.compose, "up", "--build", "-d"],
        cwd=PROJECT_DIR,
    )
    runner.ok(
        f"stack up — console http://localhost:{COMPOSE_WEB_PORT} "
        f"(API :{DEFAULT_PORT}, index :{DEFAULT_INDEX_PORT})"
    )


@runner.task("deps", "🐳", "Services", "Alias for `up` (full compose stack)")
def deps() -> None:
    up()


@runner.task(
    "stack",
    "🐳",
    "Services",
    f"Alias for `up` — open http://localhost:{COMPOSE_WEB_PORT}",
)
def stack() -> None:
    up()


@runner.task("frontend", "🌐", "Run", f"Run just the web console (Vite dev, :{WEB_PORT})")
def frontend() -> None:
    ensure_web_deps()
    port = backend_port()
    web_env = dict(os.environ)
    web_env["OBJECT_STORE_URL"] = f"http://localhost:{port}"
    runner.step(
        "🌐",
        f"starting the console on http://localhost:{WEB_PORT} "
        f"(proxying /s3 → :{port})…",
    )
    runner.run(["bun", "run", "dev"], cwd=WEB_DIR, env=web_env, check=False)


@runner.task("web-build", "🏗️", "Run", "Production build of the web console (tsc + vite build)")
def web_build() -> None:
    runner.require("bun", "Install Bun: https://bun.sh")
    ensure_web_deps()
    runner.step("🏗️", "building the web console…")
    runner.run(["bun", "run", "build"], cwd=WEB_DIR)
    runner.ok(f"built → {WEB_DIR / 'dist'}")


@runner.task(
    "bench-tier",
    "📊",
    "Bench",
    "Hot vs cold tier GET microbench (release; writes bench/results/)",
)
def bench_tier() -> None:
    runner.require("cargo", "Install Rust: https://rustup.rs")
    runner.step("📊", "hot_vs_cold (release + bench-tools)…")
    runner.run(
        [
            "cargo",
            "run",
            "--release",
            "-p",
            runner.crate,
            "--features",
            "bench-tools",
            "--bin",
            "hot_vs_cold",
        ],
        cwd=PROJECT_DIR,
    )
    runner.ok("see bench/results/ and docs/06-benchmarks.md")


@runner.task(
    "bench-haystack",
    "📊",
    "Bench",
    "FileCas vs Haystack small-object microbench (release; writes bench/results/)",
)
def bench_haystack() -> None:
    runner.require("cargo", "Install Rust: https://rustup.rs")
    runner.step("📊", "haystack_small (release + bench-tools)…")
    runner.run(
        [
            "cargo",
            "run",
            "--release",
            "-p",
            runner.crate,
            "--features",
            "bench-tools",
            "--bin",
            "haystack_small",
        ],
        cwd=PROJECT_DIR,
    )
    runner.ok("see bench/results/ and docs/06-benchmarks.md")


@runner.task("smoke", "🔥", "Run", "Hit /healthz on the backend (server must be running)")
def smoke() -> None:
    runner.require("curl", "Install curl to use this target.")
    port = backend_port()
    url = f"http://localhost:{port}/healthz"
    runner.step("🔥", f"GET {url}")
    rc = runner.run(["curl", "-sf", url], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the backend running? (make backend / make dev)")
        sys.exit(1)


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
