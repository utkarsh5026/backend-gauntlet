#!/usr/bin/env python3
"""message-broker — local dev task runner.

A small wrapper around the day-to-day commands for this project. The `Makefile`
shells out to this file so you get one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py` (Rich — auto-installed
from `tools/requirements.txt`).

There are no compose services here: the filesystem is the only dependency, which
is why the service bundle every other project registers is missing and `tree` /
`wipe` exist instead.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import shutil
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
    crate="message-broker",
    help_title="📨  message-broker",
    project_dir=PROJECT_DIR,
    default_port="9092",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Produce & fetch a batch", "make walk"),
        ("See the log on disk", "make tree"),
        ("Before you commit", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
register_smoke_healthz(runner)


def _data_dir() -> Path:
    """DATA_DIR from .env, resolved relative to the project."""
    raw = runner.load_dotenv().get("DATA_DIR", "./data")
    path = Path(raw)
    return path if path.is_absolute() else runner.project_dir / path


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("walk", "📬", "Run", "Create a topic, produce a batch, fetch it back")
def walk() -> None:
    """The end-to-end probe: the three calls the SPEC's 'What it does' promises.

    Every step past topic creation lands on a vertical you have not built yet,
    so on a fresh scaffold this fails at produce — on purpose. It turning green
    is V1 + V3 working.
    """
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    base = f"http://localhost:{port}"

    calls = [
        (
            "create topic",
            [
                "-X",
                "POST",
                f"{base}/topics",
                "-H",
                "content-type: application/json",
                "-d",
                '{"name":"orders","partitions":3}',
            ],
        ),
        (
            "produce",
            [
                "-X",
                "POST",
                f"{base}/topics/orders/records",
                "-H",
                "content-type: application/json",
                "-d",
                '{"records":[{"key":"a","value":"hello"},{"value":"world"}]}',
            ],
        ),
        ("fetch p0", [f"{base}/topics/orders/partitions/0/records?offset=0&max_records=10"]),
    ]
    for label, args in calls:
        runner.step("📬", label)
        runner.run(["curl", "-sS", "--max-time", "5", *args], check=False)
        print()


@runner.task("tree", "🌲", "Run", "Show the on-disk log layout under DATA_DIR")
def tree() -> None:
    """The broker's whole state, visible. Segment names are base offsets."""
    data = _data_dir()
    if not data.exists():
        runner.warn(f"{data} does not exist yet — run the broker and produce something")
        return
    runner.step("🌲", str(data))
    for path in sorted(data.rglob("*")):
        rel = path.relative_to(data)
        depth = len(rel.parts) - 1
        size = f"  {path.stat().st_size:>10,} B" if path.is_file() else ""
        print(f"  {'  ' * depth}{rel.name}{'/' if path.is_dir() else ''}{size}")


@runner.task("wipe", "🧨", "Run", "Delete DATA_DIR — start from an empty broker")
def wipe() -> None:
    data = _data_dir()
    if not data.exists():
        runner.warn(f"{data} does not exist — nothing to wipe")
        return
    shutil.rmtree(data)
    runner.ok(f"removed {data}")


@runner.task("profile", "🔥", "Bench", "Sample the running broker with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so drive load at the broker while
    this samples — a flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — produce some load meanwhile")
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
        "import message_broker.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run the broker")
def dev() -> None:
    sync()
    run = runner.tasks["run"][0]
    run()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
