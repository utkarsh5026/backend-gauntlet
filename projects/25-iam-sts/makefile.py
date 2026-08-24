#!/usr/bin/env python3
"""iam-sts — local dev task runner.

A small wrapper around the day-to-day commands for this project. The `Makefile`
shells out to this file so you get one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py`.

This project has **no docker dependencies** — the identity store is in memory,
because durability and replication are projects 09 and 07, and a database here
would buy nothing this SPEC grades. The only external pieces are the projects it
protects: point 23, 24 or 06 at the authorizer on :9026.

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
    crate="iam-sts",
    help_title="🔐  iam-sts",
    project_dir=PROJECT_DIR,
    default_port="9025",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Poke it", "make whoami   (signed)   ·   make unsigned   (expect 403)"),
        ("Ask a question", "make authorize"),
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


@runner.task("unsigned", "🚫", "Run", "Send an unsigned request (expect 403)")
def unsigned() -> None:
    """The fastest proof the front door is shut.

    Worth running once before anything else: if this returns anything other than
    a 403, authentication is not actually in front of the dispatcher, and every
    other test in the project is measuring the wrong thing.
    """
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    runner.step("🚫", f"GET http://localhost:{port}/?Action=GetCallerIdentity")
    runner.run(
        [
            "curl",
            "-sS",
            "-i",
            f"http://localhost:{port}/?Action=GetCallerIdentity&Version=2011-06-15",
        ],
        check=False,
    )
    print()


@runner.task("whoami", "🪪", "Run", "Signed GetCallerIdentity via botocore's signer")
def whoami() -> None:
    """Sign a real request with the bootstrap credentials and send it.

    Uses botocore because that is the bar V1 is held to: the SPEC's first
    criterion is that a request signed by a real AWS SDK verifies unmodified.
    Until V1 lands this prints the scaffold's NotImplementedError — expected.
    """
    env = runner.load_dotenv()
    port = env.get("PORT", runner.config.default_port)
    region = env.get("AWS_REGION", "us-east-1")
    key_id = env.get("BOOTSTRAP_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    secret = env.get("BOOTSTRAP_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

    runner.step("🪪", f"signed GetCallerIdentity → localhost:{port}")
    script = f"""
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

url = "http://localhost:{port}/?Action=GetCallerIdentity&Version=2011-06-15"
aws = AWSRequest(method="GET", url=url, headers={{"host": "localhost:{port}"}})
SigV4Auth(Credentials("{key_id}", "{secret}"), "sts", "{region}").add_auth(aws)
response = httpx.get(url, headers=dict(aws.headers))
print(response.status_code, response.text)
"""
    runner.uv("run", "python", "-c", script, check=False)
    print()


@runner.task("authorize", "⚖️", "Run", "Ask the authorizer a question (server must be running)")
def authorize() -> None:
    """What projects 23/24/06 send. Until V2/V5 land this is a NotImplementedError."""
    runner.require("curl", "Install curl to use this target.")
    port = runner.load_dotenv().get("AUTHZ_PORT", "9026")
    runner.step("⚖️", f"POST http://localhost:{port}/2025-01-01/authorize")
    runner.run(
        [
            "curl",
            "-sS",
            "-i",
            "-XPOST",
            f"http://localhost:{port}/2025-01-01/authorize",
            "-H",
            "content-type: application/json",
            "-d",
            '{"principal_arn":"arn:aws:iam::000000000000:user/alice",'
            '"action":"dynamodb:GetItem",'
            '"resource_arn":"arn:aws:dynamodb:us-east-1:000000000000:table/orders"}',
        ],
        check=False,
    )
    print()


@runner.task("profile", "🔥", "Bench", "Sample a running node with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so it samples a real workload rather
    than a synthetic one. Start the server, drive load at the authorizer, then run
    this.

    Read the result with this project's question in mind: how much of a decision
    is signature verification, how much is policy evaluation, and how much is
    cache lookup? The Definition of done asks you to name the split, and the
    intuitive answer is wrong often enough to be worth measuring.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some load at :9026 meanwhile")
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
        "import iam_sts.main as m; m.main()",
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
