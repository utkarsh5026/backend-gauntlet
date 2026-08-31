#!/usr/bin/env python3
"""url-shortener — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv,
migrations, the bench harness). The `Makefile` shells out to this file so you get
one source of truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

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
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_redis,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="url-shortener",
    help_title="🔗 url-shortener",
    project_dir=PROJECT_DIR,
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make deps && make migrate && make run"),
        ("See it work", "make demo"),
        ("Before you commit", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
redis = register_redis(runner, default_port=6301)
run_server = register_python_run(runner)
register_smoke_healthz(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("wait-db", "⏳", "Services", "Block until Postgres accepts connections")
def wait_db() -> None:
    runner.step("⏳", "waiting for Postgres…")
    for _ in range(30):
        probe = subprocess.run(
            [*runner.compose, "exec", "-T", "postgres", "pg_isready", "-U", "shortener"],
            cwd=str(runner.project_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            runner.ok("Postgres is ready")
            return
        time.sleep(1)
    runner.fail("Postgres did not become ready in time")
    sys.exit(1)


@runner.task("migrate", "🗃️", "Services", "Apply SQL migrations")
def migrate() -> None:
    """The Python answer to `sqlx migrate run`.

    The runner lives in `src/url_shortener/migrate.py`, so migrating needs
    nothing installed beyond this project's own dependencies — no sqlx-cli, no
    second toolchain to keep in step with the code.
    """
    runner.step("🗃️", "applying migrations…")
    runner.uv(
        "run",
        "python",
        "-m",
        "url_shortener.migrate",
        str(runner.project_dir / "migrations"),
        env=runner.load_dotenv(),
    )
    runner.ok("migrations applied")


@runner.task("up", "🐳", "Services", "Start Postgres (+ Redis only if none is already running)")
def up() -> None:
    runner.step("🐳", "starting Postgres…")
    runner.run([*runner.compose, "up", "-d", "postgres"], cwd=runner.project_dir)
    wait_db()
    redis["ensure_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("reset-db", "💥", "Services", "Drop volumes and recreate DB (destructive)")
def reset_db() -> None:
    runner.warn("dropping volumes — this wipes the database")
    runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir)
    runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
    wait_db()
    migrate()


@runner.task("dev", "🚀", "Run", "sync + deps + migrate, then run the server")
def dev() -> None:
    sync()
    deps()
    migrate()
    run_server()


@runner.task(
    "demo", "🎬", "Run", "Demo: deps + migrate + serve the dashboard (open the URL yourself)"
)
def demo() -> None:
    deps()
    migrate()
    port = runner.load_dotenv().get("PORT", "8080")
    url = f"http://localhost:{port}"
    runner.rule(C.MAGENTA)
    print(f"{C.BOLD}{C.MAGENTA}🎬  Serving the demo dashboard{C.RESET}")
    print(
        f"   Open {C.BOLD}{C.CYAN}{url}{C.RESET} once it has booted "
        f"{C.DIM}(Ctrl-C to stop the server){C.RESET}"
    )
    print(f"   {C.DIM}V1 Snowflake decode · V2 cache HIT/MISS · auth 401 · rate-limit 429{C.RESET}")
    runner.rule(C.MAGENTA)
    run_server()


@runner.task("bench", "📊", "Bench", "Micro-bench: ID generator throughput (ids/sec)")
def bench() -> None:
    runner.step("📊", "measuring id_gen throughput…")
    runner.uv("run", "python", "bench/id_gen_bench.py")


@runner.task("bench-seed", "🌱", "Bench", "Seed N bench links into Postgres (N=50000 ...)")
def bench_seed() -> None:
    runner.require("node", "Install Node.js to run the bench harness.")
    runner.step("🌱", "seeding bench links…")
    runner.run(["node", "bench/seed.js"], cwd=runner.project_dir, env=runner.load_dotenv())


@runner.task("bench-smoke", "🔥", "Bench", "Node redirect sanity check (server must run)")
def bench_smoke() -> None:
    runner.require("node", "Install Node.js to run the bench harness.")
    runner.run(["node", "bench/smoke.js"], cwd=runner.project_dir, env=runner.load_dotenv())


@runner.task("bench-load", "🏋️", "Bench", "k6 redirect load test: seed + all scenarios")
def bench_load() -> None:
    runner.require("node", "Install Node.js to run the bench harness.")
    runner.step("🏋️", "running k6 load scenarios…")
    runner.run(["node", "bench/run.js"], cwd=runner.project_dir, env=runner.load_dotenv())


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so it samples a real workload (drive
    `make bench-load` at it meanwhile) rather than a synthetic one.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some load at the server meanwhile")
    runner.uv(
        "run",
        "py-spy",
        "record",
        "--duration",
        "10",
        "--output",
        str(out),
        "--",
        "python",
        "-c",
        "import url_shortener.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
