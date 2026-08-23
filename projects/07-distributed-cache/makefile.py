#!/usr/bin/env python3
"""distributed-cache — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv, the
cluster probes). The `Makefile` shells out to this file so you get one source of
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

# The three compose nodes and their published HTTP ports (see docker-compose.yml).
CLUSTER_PORTS = {"cache-a": 8071, "cache-b": 8072, "cache-c": 8073}

runner = make_runner(
    crate="distributed-cache",
    help_title="🗄️  distributed-cache",
    project_dir=PROJECT_DIR,
    default_port="8070",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("A real 3-node cluster", "make up && make cluster"),
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


@runner.task("up", "🐳", "Services", "Build + start the 3-node cache cluster")
def up() -> None:
    runner.step("🐳", "building and starting cache-a / cache-b / cache-c…")
    runner.run([*runner.compose, "up", "-d", "--build"], cwd=runner.project_dir)
    runner.ok("cluster up — try `make cluster`")


@runner.task("cluster", "🔭", "Run", "Show every node's membership view")
def cluster() -> None:
    """The convergence probe: three views that should agree once V3 works."""
    runner.require("curl", "Install curl to use this target.")
    for name, port in CLUSTER_PORTS.items():
        runner.step("🔭", f"{name} → GET localhost:{port}/cluster")
        runner.run(
            ["curl", "-sS", "--max-time", "3", f"http://localhost:{port}/cluster"],
            check=False,
        )
        print()


@runner.task("profile", "🔥", "Bench", "Sample a running node with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to the *running* process — it does not need the server
    restarted, and it samples a real workload rather than a synthetic one.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some load at the node meanwhile")
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
        "import distributed_cache.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run a single standalone node")
def dev() -> None:
    sync()
    run = runner.tasks["run"][0]
    run()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
