#!/usr/bin/env python3
"""sqs-queue — local dev task runner.

A small wrapper around the day-to-day commands for this project. The `Makefile`
shells out to this file so you get one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py`.

This project has **no docker dependencies**: the queue store is in memory,
because durability is projects 04 and 08 and replication is 07 and 09. Adding a
database would buy nothing this SPEC grades while adding a dependency to every
test.

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
    crate="sqs-queue",
    help_title="📬  sqs-queue",
    project_dir=PROJECT_DIR,
    default_port="9029",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Poke it", "make create-queue   ·   make send   ·   make receive"),
        ("The real bar", "aws --endpoint-url http://localhost:9029 sqs list-queues"),
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


def _target(action: str, payload: str) -> None:
    """POST one SQS action the way an SDK does. The shape every poke target uses."""
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("📬", f"{action} → localhost:{port}")
    runner.run(
        [
            "curl",
            "-sS",
            "-i",
            "-XPOST",
            f"http://localhost:{port}/",
            "-H",
            f"x-amz-target: AmazonSQS.{action}",
            "-H",
            "content-type: application/x-amz-json-1.0",
            "-d",
            payload,
        ],
        check=False,
    )
    print()


@runner.task("create-queue", "🆕", "Run", "CreateQueue 'orders' (server must be running)")
def create_queue() -> None:
    """The first action, and the first `NotImplementedError` — V6 owns it."""
    _target("CreateQueue", '{"QueueName":"orders"}')


@runner.task("send", "📤", "Run", "SendMessage to 'orders'")
def send() -> None:
    """Runs through dedup, groups and the wait set — V1, V4 and V5 all touch it."""
    env = runner.load_dotenv()
    account = env.get("ACCOUNT_ID", "000000000000")
    host = env.get("ENDPOINT_HOST", f"localhost:{runner.config.default_port}")
    _target(
        "SendMessage",
        f'{{"QueueUrl":"http://{host}/{account}/orders","MessageBody":"hello"}}',
    )


@runner.task("receive", "📥", "Run", "ReceiveMessage from 'orders' (long poll)")
def receive() -> None:
    """A 5-second long poll — the fastest way to feel V3 once it is built."""
    env = runner.load_dotenv()
    account = env.get("ACCOUNT_ID", "000000000000")
    host = env.get("ENDPOINT_HOST", f"localhost:{runner.config.default_port}")
    _target(
        "ReceiveMessage",
        f'{{"QueueUrl":"http://{host}/{account}/orders",'
        f'"MaxNumberOfMessages":10,"WaitTimeSeconds":5}}',
    )


@runner.task("profile", "🔥", "Bench", "Sample a running node with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so it samples a real workload rather
    than a synthetic one. Start the server, drive the drain scenario at it, then
    run this.

    Read the result with this project's question in mind: how much of a message's
    cost is the protocol envelope (JSON parse, header handling), how much is the
    queue's own bookkeeping, and how much is the deadline engine? The Definition
    of done asks you to name the split, and the intuitive answer is wrong often
    enough to be worth measuring.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some load meanwhile")
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
        "import sqs_queue.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run the broker")
def dev() -> None:
    sync()
    runner.tasks["run"][0]()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
