#!/usr/bin/env python3
"""lsm-redis — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, docker, and the
probes that make the *storage engine* visible — which matters more here than in
any other project in the gauntlet, because the interesting state lives in files
and counters rather than in HTTP responses). The `Makefile` shells out to this
file so you get one source of truth with colors, emojis and readable output.
Help tables use `tools/makefile_help.py` (Rich — auto-installed from
`tools/requirements.txt`).

The dev loop: **your server runs locally** (`make run` → RESP on :6379, HTTP on
:8080) while the **reference redis runs in Docker** (`make up` → :6322). That
split is the whole point of the compose file: it gives you a spec-compliant
server to A/B against and the `redis-cli` / `redis-benchmark` binaries to drive
your own server with.

The probe tasks are the reason this file exists:

* `make ping`  — V1 made visible: a stock `redis-cli` against your server.
* `make data`  — V2/V4 made visible: what is actually on disk, WAL bytes and
                 SSTable count, which is where a storage engine's real state is.
* `make crash` — V2 made visible: `kill -9` the server. Restart it and see
                 whether the write you acknowledged is still there.
* `make bench` — the boss fight's load generator, pointed at your server.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    register_smoke_healthz,
)

REFERENCE_PORT = 6322
"""The reference redis's host port — 6379 with the last two digits replaced by
the project number, per the repo-wide rule. It exists so it can never collide
with your own server on 6379."""

runner = make_runner(
    crate="lsm-redis",
    help_title="🗄️  lsm-redis",
    project_dir=PROJECT_DIR,
    # The sidecar's port: `make smoke` probes /healthz, which lives on HTTP,
    # not on the RESP data plane.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Talk to your server", "make ping   (then: make cli)"),
        ("See the engine's state", "make data  ·  make stats"),
        ("Prove durability (V2)", "make crash, then make run again"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_compose_lifecycle(runner)
run_server = register_python_run(runner)
register_smoke_healthz(runner)


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _resp_port() -> int:
    return int(_env().get("RESP_PORT", "6379"))


def _http_url(path: str = "") -> str:
    port = _env().get("HTTP_PORT", runner.config.default_port)
    return f"http://localhost:{port}{path}"


def _data_dir() -> Path:
    raw = _env().get("DATA_DIR", "./data")
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_DIR / path)


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _require_server() -> None:
    """Fail with a useful hint when the server isn't up yet."""
    if not runner.port_open("127.0.0.1", _resp_port()):
        runner.fail(f"no server on :{_resp_port()} — start it with `make run`")
        sys.exit(1)


def _redis_cli(*args: str, port: int | None = None, check: bool = True) -> int:
    """Run `redis-cli` against a port.

    Prefers a locally installed `redis-cli`; falls back to the one inside the
    compose container, reaching back out to the host. That fallback is why the
    compose service sets `host.docker.internal` — a `redis-cli` you did not have
    to install is worth the indirection.
    """
    target = port if port is not None else _resp_port()
    import shutil

    if shutil.which("redis-cli") is not None:
        return runner.run(["redis-cli", "-p", str(target), *args], check=check)

    runner.warn("no local redis-cli — using the one in the compose container")
    host = "127.0.0.1" if target == REFERENCE_PORT else "host.docker.internal"
    inner = "6379" if target == REFERENCE_PORT else str(target)
    return runner.run(
        [*runner.compose, "exec", "-T", "redis", "redis-cli", "-h", host, "-p", inner, *args],
        cwd=runner.project_dir,
        check=check,
    )


# --------------------------------------------------------------------------- #
# Services — the reference redis
# --------------------------------------------------------------------------- #


@runner.task("up", "🐳", "Services", f"Start the reference redis on :{REFERENCE_PORT}")
def up() -> None:
    runner.step("🐳", "starting the reference redis…")
    runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
    print(
        f"   {C.DIM}reference server: redis-cli -p {REFERENCE_PORT} ping   "
        f"·  YOUR server is `make run`{C.RESET}"
    )


@runner.task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@runner.task("dev", "🚀", "Run", "sync + start the reference redis + run your server")
def dev() -> None:
    sync()
    up()
    run_server()


# --------------------------------------------------------------------------- #
# Probes — the criteria, made visible
# --------------------------------------------------------------------------- #


@runner.task("ping", "🏓", "Probe", "redis-cli PING + SET/GET round-trip against your server (V1)")
def ping() -> None:
    """V1's first criterion, run as a command instead of read as a sentence.

    On the bare scaffold this fails at the codec, and the *way* it fails is
    informative: the connection is accepted (so the accept loop works) and then
    drops when the first command hits `parse_command` (so V1 is what is
    missing). That is the difference between "nothing works" and "one named
    function is unwritten".
    """
    _require_server()
    runner.step("🏓", f"redis-cli -p {_resp_port()} ping / set / get")
    rc = _redis_cli("ping", check=False)
    if rc != 0:
        runner.fail("PING did not answer — the RESP codec (V1) is still a todo")
        print(f"   {C.DIM}the server log names the vertical it needs{C.RESET}")
        sys.exit(1)
    _redis_cli("set", "hello", "world", check=False)
    _redis_cli("get", "hello", check=False)
    _redis_cli("del", "hello", check=False)
    runner.ok("a stock redis-cli round-tripped a value through your engine")


@runner.task("cli", "⌨️", "Probe", "Open an interactive redis-cli on your server")
def cli() -> None:
    _redis_cli(check=False)


@runner.task("ref", "📕", "Probe", f"Open redis-cli on the reference redis (:{REFERENCE_PORT})")
def ref() -> None:
    """A/B your semantics against a real redis. When you are unsure what your
    server *should* reply — `GET` on a missing key, `DEL` of three keys where
    one exists, `SET` with a binary value — ask the one that is definitionally
    right rather than guessing."""
    _redis_cli(port=REFERENCE_PORT, check=False)


@runner.task("data", "💾", "Probe", "What's actually on disk: WAL bytes + SSTable count")
def data() -> None:
    """The storage engine's real state.

    Everything else in this project can be inspected over a socket; this cannot,
    and it is where V2 and V4 either happened or did not. A WAL that never grows
    means writes are not being logged; SSTables that never appear mean the
    memtable is never flushing; SSTables that only accumulate mean compaction is
    not keeping up — which is the write stall, visible in `ls` before it is
    visible in your latency.
    """
    directory = _data_dir()
    if not directory.exists():
        runner.warn(f"{directory} does not exist yet — run the server once")
        return

    wal = directory / "wal.log"
    ssts = sorted(directory.glob("*.sst"))
    print()
    print(f"  {C.BOLD}{directory}{C.RESET}")
    if wal.exists():
        print(f"    {C.CYAN}wal.log{C.RESET}       {wal.stat().st_size:>12,} bytes")
    else:
        print(f"    {C.DIM}wal.log        (absent){C.RESET}")
    total = 0
    for path in ssts:
        size = path.stat().st_size
        total += size
        print(f"    {C.GREEN}{path.name:<14}{C.RESET}{size:>12,} bytes")
    print()
    if not ssts:
        print(f"  {C.DIM}no SSTables yet — nothing has flushed (V4){C.RESET}")
    else:
        print(f"  {len(ssts)} SSTable(s), {total:,} bytes total")
        trigger = int(_env().get("L0_COMPACTION_TRIGGER", "4"))
        if len(ssts) > 2 * trigger:
            runner.warn(
                f"{len(ssts)} SSTables vs L0_COMPACTION_TRIGGER={trigger} — "
                "compaction is behind (this is the write stall the boss fight looks for)"
            )
    print()


@runner.task("stats", "📊", "Probe", "GET /stats — engine internals as JSON")
def stats() -> None:
    status, body = _get(_http_url("/stats"))
    if status != 200:
        runner.fail(f"unexpected status {status}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    width = max(len(k) for k in parsed)
    print()
    for key, value in parsed.items():
        print(
            f"  {C.CYAN}{key:<{width}}{C.RESET}  {value:>14,}"
            if isinstance(value, int)
            else f"  {key}  {value}"
        )
    print()
    hits, misses = parsed.get("block_cache_hits", 0), parsed.get("block_cache_misses", 0)
    if hits + misses:
        runner.ok(f"block cache hit ratio: {100 * hits / (hits + misses):.1f}%")


@runner.task("metrics", "📈", "Probe", "GET /metrics — the Prometheus scrape")
def metrics() -> None:
    _, body = _get(_http_url("/metrics"))
    lines = [
        ln for ln in body.splitlines() if ln and not ln.startswith("#") and ln.startswith("lsm_")
    ]
    if not lines:
        runner.warn("no lsm_* series yet — the metric call sites are the observability horizontal")
        return
    for line in lines:
        print(f"  {line}")
    print()
    runner.ok(f"{len(lines)} engine metric series")


@runner.task("crash", "💀", "Probe", "kill -9 the running server (V2: durability, not politeness)")
def crash() -> None:
    """The only honest durability test.

    A clean shutdown flushes; that proves nothing about `WAL_SYNC`. This sends
    SIGKILL, which the process cannot handle, cannot drain, and cannot fsync
    through — exactly what the boss fight does mid-flood. Write a key, run this,
    `make run` again, and ask for the key back. If it is gone, your `SET`
    acknowledged before the record was durable.

    Note what even this cannot test: the page cache survives `kill -9` and does
    not survive a power cut, so `WAL_SYNC=no` will *pass* this and still lose
    data on real hardware. That gap is why the policy reasoning belongs in the
    design doc and not only in a test.
    """
    runner.require("pkill", "Install procps to use this target.")
    print(f"   {C.DIM}before: `redis-cli -p {_resp_port()} set durable yes`{C.RESET}")
    runner.warn("sending SIGKILL to the server — no drain, no fsync, no mercy")
    rc = subprocess.run(["pkill", "-9", "-f", "lsm-redis"], check=False).returncode
    if rc != 0:
        runner.fail("no running lsm-redis process found")
        sys.exit(1)
    runner.ok("killed — now `make run` and ask for the key back")


@runner.task("bench", "🐉", "Bench", "redis-benchmark against your server (N=100000, P=16)")
def bench() -> None:
    """The boss fight's load generator.

    Runs inside the compose container so you do not have to install
    `redis-benchmark`, pointed back at your server on the host. Start small —
    this is the tool that produces the numbers in `docs/22-benchmarks.md`, and
    the numbers only mean something next to the hardware and the configuration
    that produced them.
    """
    _require_server()
    n = os.environ.get("N", "100000")
    pipeline = os.environ.get("P", "16")
    runner.step("🐉", f"redis-benchmark -t set,get -n {n} -P {pipeline}")
    runner.run(
        [
            *runner.compose,
            "exec",
            "-T",
            "redis",
            "redis-benchmark",
            "-h",
            "host.docker.internal",
            "-p",
            str(_resp_port()),
            "-t",
            "set,get",
            "-n",
            n,
            "-P",
            pipeline,
        ],
        cwd=runner.project_dir,
        check=False,
    )
    print(f"   {C.DIM}now check `make data` and `make stats` — did L0 stay bounded?{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    On CPython the boss fight is won or lost in the profile. py-spy attaches to
    a *running* process by PID, so start the server, drive load at it
    (`make bench` in another shell), and sample while that is happening — a
    flamegraph of an idle event loop tells you nothing.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<server pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f lsm-redis) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive load meanwhile (make bench)")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
