#!/usr/bin/env python3
"""url-shortener — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(docker, cargo, sqlx, the bench harness). The `Makefile` shells out to this file
so you get one source of truth with colors, emojis and readable output. Help
tables use `tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import sys
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
    register_postgres,
    register_redis,
    register_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="url-shortener",
    help_title="🔗 url-shortener",
    project_dir=PROJECT_DIR,
    help_footers=[
        (
            "Typical first run",
            "make setup && make deps && make migrate && make run",
        ),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_cargo_checks(runner)
register_compose_lifecycle(runner)
pg = register_postgres(runner, user="shortener", include_prepare=False)
redis = register_redis(runner, default_port=6301)
run_server = register_run(runner)
register_smoke_healthz(runner)


@runner.task(
    "up", "🐳", "Services", "Start Postgres (+ Redis only if none is already running)"
)
def up() -> None:
    runner.step("🐳", "starting Postgres…")
    runner.run([*runner.compose, "up", "-d", "postgres"], cwd=runner.project_dir)
    pg["wait_db"]()
    redis["ensure_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("dev", "🚀", "Run", "Start deps, migrate, then run server")
def dev() -> None:
    deps()
    pg["migrate"]()
    run_server()


@runner.task(
    "demo",
    "🎬",
    "Run",
    "Demo: deps + migrate + serve the dashboard (open the URL yourself)",
)
def demo() -> None:
    deps()
    pg["migrate"]()
    port = runner.load_dotenv().get("PORT", "8080")
    url = f"http://localhost:{port}"
    runner.rule(C.MAGENTA)
    print(f"{C.BOLD}{C.MAGENTA}🎬  Serving the demo dashboard{C.RESET}")
    print(
        f"   Open {C.BOLD}{C.CYAN}{url}{C.RESET} once it has booted "
        f"{C.DIM}(Ctrl-C to stop the server){C.RESET}"
    )
    print(
        f"   {C.DIM}V1 Snowflake decode · V2 cache HIT/MISS · "
        f"auth 401 · rate-limit 429{C.RESET}"
    )
    runner.rule(C.MAGENTA)
    run_server()


@runner.task("bench", "📊", "Bench", "Criterion micro-bench: ID generator throughput")
def bench() -> None:
    runner.step("📊", "running id_gen criterion bench…")
    runner.cargo("bench", "-p", runner.crate, "--bench", "id_gen")


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


register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
