#!/usr/bin/env python3
"""lambda-compute — local dev task runner.

A small wrapper around the day-to-day commands for this project. The `Makefile`
shells out to this file so you get one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py`.

This project has **no docker dependencies** — it is the compute plane — so there
are no compose tasks here. Sandboxes are local processes under `SANDBOX_ROOT`.
V6's poller reads from project 23 if you have it running; nothing else needs it.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    make_runner,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="lambda-compute",
    help_title="λ  lambda-compute",
    project_dir=PROJECT_DIR,
    default_port="9001",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Poke it", "make register && make invoke"),
        ("Before you commit", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
register_smoke_healthz(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("register", "λ", "Run", "Register a demo function (server must be running)")
def register() -> None:
    """The fastest way to confirm the control plane works end to end."""
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("λ", f"POST http://localhost:{port}/2015-03-31/functions")
    runner.run(
        [
            "curl",
            "-sS",
            "-XPOST",
            f"http://localhost:{port}/2015-03-31/functions",
            "-H",
            "content-type: application/json",
            "-d",
            '{"FunctionName":"hello","Handler":"examples.hello.handler",'
            '"MemorySize":128,"Timeout":3}',
        ],
        check=False,
    )
    print()


@runner.task("invoke", "🚀", "Run", "Invoke the demo function synchronously")
def invoke() -> None:
    """Until V1-V4 land this returns the scaffold's NotImplementedError — expected."""
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("🚀", f"POST http://localhost:{port}/2015-03-31/functions/hello/invocations")
    runner.run(
        [
            "curl",
            "-sS",
            "-i",
            "-XPOST",
            f"http://localhost:{port}/2015-03-31/functions/hello/invocations",
            "-H",
            "content-type: application/json",
            "-d",
            '{"name":"world"}',
        ],
        check=False,
    )
    print()


@runner.task("profile", "🔥", "Bench", "Sample a running node with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so it samples a real workload rather
    than a synthetic one. Start the server, drive load at it, then run this.

    Read the result with this project's question in mind: how much of the warm-path
    cost is the supervisor's rather than the function's? Only one of those is yours
    to fix, and the Definition of done asks you to say which.
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
        "import lambda_compute.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run the node")
def dev() -> None:
    sync()
    runner.tasks["run"][0]()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
