#!/usr/bin/env python3
"""realtime-pubsub — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(docker, cargo, the bench harness). The `Makefile` shells out to this file
so you get one source of truth with colors, emojis and readable output. Help
tables use `tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

`make dev` uses ``register_dev_stack`` (shared with root ``tools/dev.py``): it
auto-detects Docker Compose, the Rust server, and ``web/``, then launches them
together in mprocs.

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
    register_dev_stack,
    register_help,
    register_redis,
    register_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="realtime-pubsub",
    help_title="📡 realtime-pubsub",
    project_dir=PROJECT_DIR,
    help_footers=[
        (
            "Full stack (Redis + server + playground)",
            "make dev   → open http://localhost:5173",
        ),
        (
            "Single node, no Redis",
            "make run   (V1–V3) · make frontend for the UI alone",
        ),
        ("Multi-node (V4)", "CLUSTER=true, run two nodes on different PORTs"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_cargo_checks(runner)
register_compose_lifecycle(runner)
redis = register_redis(runner, default_port=6303)
register_run(runner)
register_smoke_healthz(runner)


@runner.task("up", "🐳", "Services", "Start Redis (the cross-node bus, only needed for V4)")
def up() -> None:
    redis["ensure_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task(
    "obs-up", "📊", "Services", "Start Prometheus (:9003) + Grafana (:3003)"
)
def obs_up() -> None:
    runner.step("📊", "starting Prometheus + Grafana…")
    runner.run(
        [*runner.compose, "up", "-d", "prometheus", "grafana"],
        cwd=runner.project_dir,
    )
    runner.ok(
        "Grafana → http://localhost:3003  ·  Prometheus → http://localhost:9003"
    )
    runner.warn("run the app (make run / make dev) so there's something to scrape")


@runner.task(
    "obs-down",
    "🛑",
    "Services",
    "Stop Prometheus + Grafana (leaves Postgres/Redis running)",
)
def obs_down() -> None:
    runner.step("🛑", "stopping Prometheus + Grafana…")
    runner.run(
        [*runner.compose, "stop", "prometheus", "grafana"],
        cwd=runner.project_dir,
    )
    runner.ok("observability stopped")


# Auto-detects compose + server + web/ → mprocs (also registers frontend / web-install).
register_dev_stack(runner)

register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
