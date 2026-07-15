#!/usr/bin/env python3
"""rate-limiter — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(docker, cargo, the gRPC smoke probe). The `Makefile` shells out to this file
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
    make_runner,
    register_cargo_checks,
    register_compose_lifecycle,
    register_help,
    register_redis,
    register_run,
    register_setup,
)

# gRPC service coordinates for the `smoke` probe (see proto/ratelimit.proto).
PROTO = "ratelimit.proto"
GRPC_SERVICE = "ratelimit.v1.RateLimiter"

runner = make_runner(
    crate="rate-limiter",
    help_title="🚦 rate-limiter",
    project_dir=PROJECT_DIR,
    help_footers=[
        ("Typical first run", "make setup && make deps && make run"),
        ("Run all checks", "make verify"),
    ],
    default_port="50051",
)

register_setup(runner)
register_cargo_checks(runner)
register_compose_lifecycle(runner)
redis = register_redis(runner, default_port=6302, include_reset=True)
register_run(runner)


@runner.task("up", "🐳", "Services", "Start Redis (docker compose up -d)")
def up() -> None:
    runner.step("🐳", "starting Redis…")
    runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
    redis["wait_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("dev", "🚀", "Run", "Start Redis, then run the server")
def dev() -> None:
    deps()
    runner.tasks["run"][0]()


@runner.task("smoke", "🔥", "Run", "gRPC Peek probe (server must be running; needs grpcurl)")
def smoke() -> None:
    runner.require("grpcurl", "Install grpcurl: https://github.com/fullstorydev/grpcurl")
    port = runner.load_dotenv().get("PORT", "50051")
    runner.step("🔥", f"gRPC {GRPC_SERVICE}/Peek on localhost:{port}")
    rc = runner.run(
        [
            "grpcurl",
            "-plaintext",
            "-import-path",
            "proto",
            "-proto",
            PROTO,
            "-d",
            '{"key": "smoke"}',
            f"localhost:{port}",
            f"{GRPC_SERVICE}/Peek",
        ],
        cwd=runner.project_dir,
        check=False,
    )
    if rc == 0:
        runner.ok("Peek OK")
    else:
        runner.fail("Peek failed — is the server running?")
        sys.exit(1)


register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
