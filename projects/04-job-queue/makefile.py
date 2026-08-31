#!/usr/bin/env python3
"""job-queue — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv,
migrations). The `Makefile` shells out to this file so you get one source of truth
with colors, emojis and readable output. Help tables use `tools/makefile_help.py`
(Rich — auto-installed from `tools/requirements.txt`).

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
    make_runner,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="job-queue",
    help_title="📬 job-queue",
    project_dir=PROJECT_DIR,
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make deps && make migrate && make run"),
        ("Drain jobs too", "RUN_WORKERS=true make run  (workers are off by default)"),
        ("Run all checks", "make verify"),
        ("Prod-parity: run the app in Docker too", "make dev-container"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
register_python_run(runner)
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
            [*runner.compose, "exec", "-T", "postgres", "pg_isready", "-U", "jobs"],
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

    The runner lives in `src/job_queue/migrate.py`, so migrating needs nothing
    installed beyond this project's own dependencies — no sqlx-cli, no second
    toolchain to keep in step with the code.
    """
    runner.step("🗃️", "applying migrations…")
    runner.uv(
        "run",
        "python",
        "-m",
        "job_queue.migrate",
        str(runner.project_dir / "migrations"),
        env=runner.load_dotenv(),
    )
    runner.ok("migrations applied")


@runner.task("up", "🐳", "Services", "Start Postgres (store + queue broker)")
def up() -> None:
    runner.step("🐳", "starting Postgres…")
    runner.run([*runner.compose, "up", "-d", "postgres"], cwd=runner.project_dir)
    wait_db()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("reset-db", "💥", "Services", "Drop volumes and recreate the DB (destructive)")
def reset_db() -> None:
    runner.warn("dropping volumes — this wipes the database")
    runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir, check=False)
    up()
    migrate()


@runner.task("db-ui", "🔭", "Services", "Open pgweb — browse tables/rows at http://localhost:8004")
def db_ui() -> None:
    runner.step("🔭", "starting pgweb (Postgres browser UI)…")
    runner.run([*runner.compose, "up", "-d", "pgweb"], cwd=runner.project_dir)
    runner.ok("pgweb is up → http://localhost:8004")


@runner.task("dev", "🚀", "Run", "Start deps, migrate, then run the server")
def dev() -> None:
    deps()
    migrate()
    runner.tasks["run"][0]()


@runner.task(
    "dev-container",
    "🐋",
    "Run",
    "Prod-parity loop: deps, migrate, then run the app itself in Docker",
)
def dev_container() -> None:
    deps()
    migrate()
    runner.step("🐋", "building + starting job-queue in Docker…")
    runner.run(
        [*runner.compose, "up", "-d", "--build", "job-queue"],
        cwd=runner.project_dir,
    )
    runner.ok("job-queue is up → http://localhost:8080 (make logs to follow it)")


@runner.task("profile", "🔥", "Bench", "Sample a running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so point load at the server first — a
    flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some enqueue/drain load meanwhile")
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
        "import job_queue.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
