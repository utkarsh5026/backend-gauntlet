#!/usr/bin/env python3
"""raft-kv — local dev task runner.

A small wrapper around the day-to-day commands for this project. The `Makefile`
shells out to this file so you get one source of truth with colors, emojis and
readable output. Help tables use `tools/makefile_help.py` (Rich — auto-installed
from `tools/requirements.txt`).

There are no compose services here: each node persists to the filesystem and
reaches the others over HTTP, so the only dependency is more copies of itself.
That is why `cluster` / `watch` / `wipe` exist where other projects have a service
bundle.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
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
    crate="raft-kv",
    help_title="🗳️  raft-kv",
    project_dir=PROJECT_DIR,
    # Node 1's port in the default PEERS. `make run` with a different NODE_ID
    # binds that node's port instead — the topology comes from PEERS, not here.
    default_port="9001",
    help_footers=[
        ("Typical first run", "make setup && make sync && make cluster"),
        ("Watch the cluster", "make watch"),
        ("Write and read a key", "make walk"),
        ("Before you commit", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
register_smoke_healthz(runner)


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _cluster() -> dict[int, str]:
    """The PEERS map from .env, as {node_id: host:port}."""
    raw = _env().get("PEERS", "1=127.0.0.1:9001")
    cluster: dict[int, str] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if entry and "=" in entry:
            node_id, _, addr = entry.partition("=")
            cluster[int(node_id.strip())] = addr.strip()
    return cluster


def _port(addr: str) -> str:
    return addr.rpartition(":")[2]


def _data_dir() -> Path:
    """DATA_DIR from .env, resolved relative to the project."""
    path = Path(_env().get("DATA_DIR", "./data"))
    return path if path.is_absolute() else runner.project_dir / path


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("cluster", "🗳️", "Run", "Run every node in PEERS in one terminal")
def cluster() -> None:
    """Boot the whole cluster as child processes, streaming their logs together.

    A Raft cluster is N copies of one binary sharing one PEERS map, so this is
    genuinely all it takes. Interleaved logs are the point: an election is a
    conversation, and reading one node's half of it tells you very little.

    Ctrl-C stops all of them. Each node gets its own NODE_ID; everything else is
    inherited from .env.
    """
    env = _env()
    nodes = sorted(_cluster())
    if len(nodes) < 2:
        runner.warn(f"PEERS names only {len(nodes)} node — set a 3-node PEERS in .env")

    procs: list[subprocess.Popen[bytes]] = []
    runner.step("🗳️", f"starting {len(nodes)} nodes: {', '.join(str(n) for n in nodes)}")
    for node_id in nodes:
        child_env = {**os.environ, **env, "NODE_ID": str(node_id)}
        procs.append(
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                ["uv", "run", "raft-kv"],
                cwd=runner.project_dir,
                env=child_env,
            )
        )
        # Stagger the starts slightly so the log lines of three simultaneous
        # boots don't shred each other on the way out.
        time.sleep(0.2)

    try:
        while any(p.poll() is None for p in procs):
            time.sleep(0.3)
    except KeyboardInterrupt:
        runner.step("🛑", "stopping cluster…")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        runner.ok("cluster stopped")


@runner.task("watch", "👀", "Run", "Poll /status on every node until Ctrl-C")
def watch() -> None:
    """The cluster's state, refreshed in place.

    This is the tool for V1: run `make cluster` in one terminal and this in
    another, and an election is something you *watch* rather than something you
    reconstruct from logs afterwards.
    """
    runner.require("curl", "Install curl to use this target.")
    nodes = sorted(_cluster().items())
    try:
        while True:
            lines = [f"  {'node':<6}{'role':<12}{'term':<7}{'leader':<8}{'commit':<8}applied"]
            for node_id, addr in nodes:
                probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["curl", "-sf", "--max-time", "1", f"http://{addr}/status"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if probe.returncode != 0:
                    lines.append(f"  {node_id:<6}{'(down)':<12}")
                    continue
                import json

                body = json.loads(probe.stdout)
                lines.append(
                    f"  {node_id:<6}{body['role']:<12}{body['term']:<7}"
                    f"{str(body['leader_id']):<8}{body['commit_index']:<8}{body['last_applied']}"
                )
            print("\033[2J\033[H" + "\n".join(lines), flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()


@runner.task("walk", "🔑", "Run", "Write a key, read it back, delete it")
def walk() -> None:
    """The three calls the SPEC's 'What it does' promises.

    Every one of them lands on a vertical you have not built yet, so on a fresh
    scaffold this fails at the first write — on purpose. It turning green is
    V1 + V2 + V3 working, and `-L` means a write sent to a follower follows the
    leader redirect, which is the protocol horizontal item in action.
    """
    runner.require("curl", "Install curl to use this target.")
    port = _port(next(iter(_cluster().values())))
    base = f"http://localhost:{port}"

    calls = [
        ("status", [f"{base}/status"]),
        (
            "put /kv/hello",
            [
                "-X",
                "PUT",
                f"{base}/kv/hello",
                "-H",
                "content-type: application/json",
                "-d",
                '{"value":"world"}',
            ],
        ),
        ("get /kv/hello", [f"{base}/kv/hello"]),
        ("delete /kv/hello", ["-X", "DELETE", f"{base}/kv/hello"]),
    ]
    for label, args in calls:
        runner.step("🔑", label)
        runner.run(["curl", "-sSL", "--max-time", "5", *args], check=False)
        print()


@runner.task("wipe", "🧨", "Run", "Delete DATA_DIR — every node forgets everything")
def wipe() -> None:
    """Wipe the persistent state of every node.

    Worth understanding what this destroys: the term, the vote and the log. A
    cluster restarted from an empty DATA_DIR is not a restarted cluster, it is a
    brand new one — which is exactly why it is a separate, explicit target and not
    part of `make run`.
    """
    data = _data_dir()
    if not data.exists():
        runner.warn(f"{data} does not exist — nothing to wipe")
        return
    shutil.rmtree(data)
    runner.ok(f"removed {data}")


@runner.task("profile", "🔥", "Bench", "Sample a running node with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so drive write load at the leader
    while this samples — a flamegraph of an idle election timer tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some writes meanwhile")
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
        "import raft_kv.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run the whole cluster")
def dev() -> None:
    sync()
    cluster()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
