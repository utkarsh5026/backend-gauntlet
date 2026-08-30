#!/usr/bin/env python3
"""metrics-pipeline — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv, the
ingest probes). The `Makefile` shells out to this file so you get one source of
truth with colors, emojis and readable output. Help tables use
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
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
    register_smoke_healthz,
)

# A sample line-protocol payload, used by `make send`. Two fields on one line, so
# it exercises the "one point per field" rule the V1 parser has to get right.
SAMPLE_LINE = "cpu,host=a,region=us usage=0.91,sys=0.12"

runner = make_runner(
    crate="metrics-pipeline",
    help_title="📈  metrics-pipeline",
    project_dir=PROJECT_DIR,
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make up && make run"),
        ("Send a point", "make send"),
        ("Watch the live feed", "make stream"),
        ("Before you commit", "make verify"),
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


@runner.task("up", "🐳", "Services", "Start NATS (JetStream) + ClickHouse")
def up() -> None:
    runner.step("🐳", "starting nats + clickhouse…")
    runner.run([*runner.compose, "up", "-d", "--wait"], cwd=runner.project_dir)
    runner.ok("dependencies up")


@runner.task("schema", "🗄️", "Services", "Apply the ClickHouse rollup schema by hand")
def schema() -> None:
    """The compose file mounts the DDL into the container's init dir, so a fresh
    volume applies it automatically. This is for re-applying it to a live one.
    """
    sql = (runner.project_dir / "migrations" / "0001_init.sql").read_text()
    runner.step("🗄️", "applying migrations/0001_init.sql…")
    runner.run(
        [
            *runner.compose,
            "exec",
            "-T",
            "clickhouse",
            "clickhouse-client",
            "-n",
            "--query",
            sql,
        ],
        cwd=runner.project_dir,
    )
    runner.ok("schema applied")


@runner.task("send", "📤", "Run", "POST a sample line-protocol point to /ingest")
def send() -> None:
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("📤", f"POST http://localhost:{port}/ingest")
    runner.run(
        [
            "curl",
            "-sS",
            "-i",
            "-X",
            "POST",
            f"http://localhost:{port}/ingest",
            "--data-binary",
            SAMPLE_LINE,
        ],
        check=False,
    )
    print()


@runner.task("stream", "📡", "Run", "Follow the SSE live feed (GET /stream)")
def stream() -> None:
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("📡", f"GET http://localhost:{port}/stream — ctrl-c to stop")
    runner.run(["curl", "-N", f"http://localhost:{port}/stream"], check=False)


@runner.task("profile", "🔥", "Bench", "Sample a running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so it samples a real firehose rather
    than a synthetic one: start the server, point load at it, then run this.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some ingest load meanwhile")
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
        "import metrics_pipeline.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + deps up + run the server")
def dev() -> None:
    sync()
    up()
    run = runner.tasks["run"][0]
    run()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
