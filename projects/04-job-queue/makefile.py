#!/usr/bin/env python3
"""job-queue — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(docker, cargo, sqlx). The `Makefile` shells out to this file so you get one
source of truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

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
    make_runner,
    register_cargo_checks,
    register_compose_lifecycle,
    register_help,
    register_postgres,
    register_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="job-queue",
    help_title="📬 job-queue",
    project_dir=PROJECT_DIR,
    help_footers=[
        (
            "Typical first run",
            "make setup && make deps && make migrate && make prepare && make run",
        ),
        ("Run all checks", "make verify"),
        ("Prod-parity: run the app in Docker too", "make dev-container"),
    ],
)

register_setup(runner)
register_cargo_checks(runner)
register_compose_lifecycle(runner)
pg = register_postgres(runner, user="jobs")
register_run(runner)
register_smoke_healthz(runner)


@runner.task("up", "🐳", "Services", "Start Postgres (store + queue broker)")
def up() -> None:
    runner.step("🐳", "starting Postgres…")
    runner.run([*runner.compose, "up", "-d", "postgres"], cwd=runner.project_dir)
    pg["wait_db"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task(
    "db-ui",
    "🔭",
    "Services",
    "Open pgweb — browse tables/rows at http://localhost:8004",
)
def db_ui() -> None:
    runner.step("🔭", "starting pgweb (Postgres browser UI)…")
    runner.run([*runner.compose, "up", "-d", "pgweb"], cwd=runner.project_dir)
    runner.ok("pgweb is up → http://localhost:8004")


@runner.task("dev", "🚀", "Run", "Start deps, migrate, then run server")
def dev() -> None:
    deps()
    pg["migrate"]()
    runner.tasks["run"][0]()


@runner.task(
    "dev-container",
    "🐋",
    "Run",
    "Prod-parity loop: deps, migrate, then run the app itself in Docker",
)
def dev_container() -> None:
    deps()
    pg["migrate"]()
    runner.step("🐋", "building + starting job-queue in Docker…")
    runner.run(
        [*runner.compose, "up", "-d", "--build", "job-queue"],
        cwd=runner.project_dir,
    )
    runner.ok("job-queue is up → http://localhost:8080 (make logs to follow it)")


register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
