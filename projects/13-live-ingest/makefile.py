#!/usr/bin/env python3
"""live-ingest — local dev task runner.

A small wrapper around the day-to-day commands for this crate (cargo, the server,
the RTMP end-to-end smoke test). The `Makefile` shells out to this file so you get
one source of truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

There is deliberately **no docker/database** here: the source is a live RTMP socket
and everything downstream lives in a bounded in-memory window (see SPEC.md), so this
runner has no compose/migrate/bench bundles — just checks, run, and the smoke tests.

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
    register_dev_stack,
    register_help,
    register_md,
    register_run,
    register_setup,
)

runner = make_runner(
    crate="live-ingest",
    help_title="🎥 live-ingest (RTMP → LL-HLS)",
    project_dir=PROJECT_DIR,
    help_footers=[
        ("Typical first run", "make setup && make run"),
        ("Run all checks", "make verify"),
        ("Prove the ingest path", "make smoke-rtmp  (needs ffmpeg)"),
    ],
    default_port="8080",  # HTTP delivery port (RTMP ingest is RTMP_PORT=1935)
)

register_setup(runner)
register_cargo_checks(runner)
register_run(runner)
# web/ exists → auto full-stack `dev` (server + Vite) plus `web-install` / `frontend`.
register_dev_stack(runner)


@runner.task("smoke", "🔥", "Run", "Hit /healthz on HTTP_PORT (server must be running)")
def smoke() -> None:
    runner.require("curl", "Install curl to use this target.")
    # This project uses HTTP_PORT (not the generic PORT) for delivery.
    port = runner.load_dotenv().get("HTTP_PORT", runner.config.default_port)
    runner.step("🔥", f"GET http://localhost:{port}/healthz")
    rc = runner.run(["curl", "-sf", f"http://localhost:{port}/healthz"], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


@runner.task(
    "smoke-rtmp",
    "📡",
    "Run",
    "End-to-end RTMP ingest test: build+start server, push ffmpeg, assert handshake",
)
def smoke_rtmp() -> None:
    # Self-contained: the script builds/starts the server and cleans it up itself,
    # so nothing needs to be running first. Requires ffmpeg (see script for install).
    script = runner.project_dir / "scripts" / "smoke_rtmp.py"
    runner.step("📡", "running RTMP handshake + chunk-reader smoke test…")
    rc = runner.run([sys.executable, str(script)], cwd=runner.project_dir, check=False)
    if rc != 0:
        sys.exit(rc)


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
