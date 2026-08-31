#!/usr/bin/env python3
"""realtime-pubsub — local dev task runner.

A small wrapper around the day-to-day commands for this project (docker, uv, the
websocket probes). The `Makefile` shells out to this file so you get one source
of truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

`make dev` uses ``register_dev_stack`` (shared with root ``tools/dev.py``): it
auto-detects Docker Compose, the Python server, and ``web/``, then launches them
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
    register_compose_lifecycle,
    register_dev_stack,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_redis,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="realtime-pubsub",
    help_title="📡  realtime-pubsub",
    project_dir=PROJECT_DIR,
    default_port="8080",
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
register_python_checks(runner)
register_compose_lifecycle(runner)
redis = register_redis(runner, default_port=6303)
register_python_run(runner)
register_smoke_healthz(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("up", "🐳", "Services", "Start Redis (the cross-node bus, only needed for V4)")
def up() -> None:
    redis["ensure_redis"]()


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("db", "🐘", "Services", "Start Postgres (the optional /admin roster)")
def db() -> None:
    runner.step("🐘", "starting postgres…")
    runner.run([*runner.compose, "up", "-d", "--wait", "postgres"], cwd=runner.project_dir)
    runner.ok("postgres up — set DATABASE_URL in .env to enable /admin")


@runner.task("obs-up", "📊", "Services", "Start Prometheus (:9003) + Grafana (:3003)")
def obs_up() -> None:
    runner.step("📊", "starting Prometheus + Grafana…")
    runner.run(
        [*runner.compose, "up", "-d", "prometheus", "grafana"],
        cwd=runner.project_dir,
    )
    runner.ok("Grafana → http://localhost:3003  ·  Prometheus → http://localhost:9003")
    runner.warn("run the app (make run / make dev) so there's something to scrape")


@runner.task(
    "obs-down",
    "🛑",
    "Services",
    "Stop Prometheus + Grafana (leaves Postgres/Redis running)",
)
def obs_down() -> None:
    runner.step("🛑", "stopping Prometheus + Grafana…")
    runner.run([*runner.compose, "stop", "prometheus", "grafana"], cwd=runner.project_dir)
    runner.ok("observability stopped")


@runner.task("ws", "🔌", "Run", "Open a websocket to /ws with websocat")
def ws() -> None:
    """A hand-driven client. Paste frames like:

    {"type":"subscribe","topic":"room1"}
    {"type":"publish","topic":"room1","payload":{"hello":"world"}}
    """
    runner.require("websocat", "Install with: cargo install websocat")
    env = runner.load_dotenv()
    port = env.get("PORT", runner.config.default_port)
    token = env.get("WS_AUTH_TOKEN", "")
    if not token:
        runner.warn("WS_AUTH_TOKEN is empty in .env — the upgrade will be rejected")
    runner.step("🔌", f"ws://localhost:{port}/ws — ctrl-c to stop")
    runner.run(
        ["websocat", f"ws://localhost:{port}/ws?token={token}&identity=cli"],
        check=False,
    )


@runner.task("profile", "🔥", "Bench", "Sample a running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so point load at the server first —
    a flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some fan-out load meanwhile")
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
        "import realtime_pubsub.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


# Auto-detects compose + server + web/ → mprocs (also registers frontend / web-install).
register_dev_stack(runner, use_cargo_watch=False)

register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
