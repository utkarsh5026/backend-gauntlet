#!/usr/bin/env python3
"""live-platform — local dev task runner.

A wrapper around the day-to-day commands for this project: uv, the three
docker-compose dependencies, migrations, and probes that make each plane visible.
The `Makefile` shells out to this file so there is one source of truth with
colors, emojis and readable output. Help tables use `tools/makefile_help.py`.

The probes are ordered the way the SPEC's suggested order of attack is:

* `make ingest` — V1 made visible: the webhook an ingest edge fires when a
                  broadcaster connects, for the seeded `demo` key.
* `make play`   — V3 made visible: the master playlist a player asks for first.
* `make status` — the registry, the ladder, transcode backlog, chat channels.

On the bare scaffold both probes answer `501` with the todo that blocks them,
which is the fastest way to see which vertical you are on.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_compose_lifecycle,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

DEMO_KEY = "demo"
"""A dev-only stream key, seeded by `make seed`. Ingest rejects unregistered keys,
so without a seeded row every ingest probe is (correctly) a rejection."""

runner = make_runner(
    crate="live-platform",
    help_title="📺 live-platform (control plane · transcode autoscale · LL-HLS edge · chat)",
    project_dir=PROJECT_DIR,
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make dev"),
        ("Start the three deps", "make up"),
        ("Fire the ingest webhook (V1)", "make ingest"),
        ("Ask for the playlist (V3)", "make play"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
register_python_run(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _url(path: str = "") -> str:
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _request(url: str, *, body: dict[str, str] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _report(code: int, body: str) -> None:
    if code == 0:
        runner.fail(f"no answer — is the server running? ({body})")
        sys.exit(1)
    color = C.GREEN if code < 400 else C.YELLOW if code == 501 else C.RED
    print(f"  {color}{code}{C.RESET}  {body.strip()}")
    if code == 501:
        print(f"   {C.DIM}that todo is the worklist{C.RESET}")
    print()


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


@runner.task("up", "🐳", "Services", "Start postgres + redis + nats and wait until healthy")
def up() -> None:
    runner.step("🐳", "starting postgres, redis, nats…")
    # `--wait` blocks on each service's compose healthcheck, so this returns only
    # once all three actually accept connections — not merely once they started.
    runner.run([*runner.compose, "up", "-d", "--wait"], cwd=runner.project_dir)
    runner.ok("deps healthy → postgres :5416  redis :6316  nats :4216")


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("migrate", "🗃️", "Services", "Apply SQL migrations")
def migrate() -> None:
    """The Python answer to `sqlx migrate run` — see `live_platform.migrate`."""
    runner.step("🗃️", "applying migrations…")
    runner.uv(
        "run",
        "python",
        "-m",
        "live_platform.migrate",
        str(runner.project_dir / "migrations"),
        env=runner.load_dotenv(),
    )
    runner.ok("migrations applied")


@runner.task("seed", "🌱", "Services", f"Register the dev stream key '{DEMO_KEY}'")
def seed() -> None:
    runner.step("🌱", f"registering stream key '{DEMO_KEY}'…")
    sql = (
        "INSERT INTO streams (stream_key, owner) "
        f"VALUES ('{DEMO_KEY}', 'dev') ON CONFLICT (stream_key) DO NOTHING"
    )
    runner.run(
        [
            *runner.compose,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "live",
            "-d",
            "live",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=runner.project_dir,
    )
    runner.ok(f"'{DEMO_KEY}' is registered")


@runner.task("reset-db", "💥", "Services", "Drop volumes, recreate, migrate, seed (destructive)")
def reset_db() -> None:
    runner.warn("dropping volumes — this wipes every stream, session and queued job")
    runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir, check=False)
    up()
    migrate()
    seed()


@runner.task("dev", "🚀", "Run", "Deps up, migrate, seed, then run the server")
def dev() -> None:
    deps()
    migrate()
    seed()
    runner.tasks["run"][0]()


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


@runner.task("smoke", "🔥", "Run", "Hit /healthz (server must be running)")
def smoke() -> None:
    runner.step("🔥", f"GET {_url('/healthz')}")
    code, body = _request(_url("/healthz"))
    if code == 200:
        runner.ok(f"healthz OK ({body.strip()})")
    else:
        runner.fail(f"healthz failed ({code}) — is the server running?")
        sys.exit(1)


@runner.task("status", "📊", "Probe", "GET /status — registry, ladder, backlog, chat")
def status() -> None:
    code, body = _request(_url("/status"))
    if code != 200:
        _report(code, body)
        return
    parsed = json.loads(body)
    print()
    print(f"  {C.BOLD}live{C.RESET}         {parsed['streams_live']} of {len(parsed['streams'])}")
    ladder = "  ".join(f"{r['name']}@{r['bitrate_kbps']}k" for r in parsed["ladder"])
    print(f"  {C.BOLD}ladder{C.RESET}       {ladder}")
    print(
        f"  {C.BOLD}segments{C.RESET}     {parsed['segment_secs']}s, parts {parsed['part_secs']}s"
    )
    print(f"  {C.BOLD}transcode{C.RESET}    {parsed['transcode']}")
    print(f"  {C.BOLD}chat{C.RESET}         {parsed['chat']}")
    print(f"  {C.BOLD}origin{C.RESET}       {parsed['edge_origin']}")
    for error in parsed["background_errors"]:
        print(f"  {C.BOLD}{C.RED}dead task{C.RESET}    {error}")
    print()


@runner.task("metrics", "📈", "Probe", "GET /metrics — the live_* series")
def metrics() -> None:
    _, body = _request(_url("/metrics"))
    lines = [ln for ln in body.splitlines() if ln.startswith("live_")]
    if not lines:
        runner.warn("no live_* series — is the server running?")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} platform metric series")


@runner.task("ingest", "📡", "Probe", f"POST /ingest/start for '{DEMO_KEY}' (V1)")
def ingest() -> None:
    """V1's first criterion, run as a command rather than read as a sentence."""
    payload = {"stream_key": DEMO_KEY, "ingest_node": os.environ.get("NODE", "node-dev")}
    runner.step("📡", f"POST {_url('/ingest/start')}")
    _report(*_request(_url("/ingest/start"), body=payload))


@runner.task("play", "▶️", "Probe", f"GET the '{DEMO_KEY}' master playlist (V3)")
def play() -> None:
    url = _url(f"/live/{DEMO_KEY}/master.m3u8")
    runner.step("▶️", f"GET {url}")
    _report(*_request(url))


# --------------------------------------------------------------------------- #
# The boss fight
# --------------------------------------------------------------------------- #


@runner.task("bench", "🐉", "Bench", "The Viral Spike: 200 → 100k viewers in 30s")
def bench() -> None:
    """The boss fight's harness — building it is part of the fight.

    The Arena is a local k8s cluster with the HPA active, so this cannot be one
    command until you have built `k8s/` and `bench/`. Three scenarios run
    together: the viewer ramp, the cold-partial stampede, and the chat firehose.
    """
    runner.warn("bench/ and k8s/ are yours to build — see the 🐉 Boss fight in SPEC.md")
    print(f"   {C.DIM}make md       # read the Arena + 'the boss falls when' lines{C.RESET}")
    print(f"   {C.DIM}while it runs: make metrics | grep -E 'fills|replicas|slow'{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process by PID, so start the server, drive
    real load at it from another shell, and sample while that happens — a
    flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<server pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f live-platform) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive load meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
