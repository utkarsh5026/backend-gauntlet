#!/usr/bin/env python3
"""realtime-pubsub — local dev task runner.

A small wrapper around the day-to-day commands for this crate
(docker, cargo, the bench harness). The `Makefile` shells out to this file
so you get one source of truth with colors, emojis and readable output. Help
tables use `tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR.parent.parent
if str(WORKSPACE / "tools") not in sys.path:
    sys.path.insert(0, str(WORKSPACE / "tools"))
from makefile_help import print_project_help  # noqa: E402
CRATE = "realtime-pubsub"
COMPOSE = ["docker", "compose"]
ENV_FILE = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"


class C:
    """ANSI styles, auto-disabled when stdout is not a TTY or NO_COLOR is set."""

    _on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    MAGENTA = "\033[35m" if _on else ""
    CYAN = "\033[36m" if _on else ""


def step(emoji: str, msg: str) -> None:
    print(f"{C.CYAN}{emoji}  {msg}{C.RESET}")


def ok(msg: str) -> None:
    print(f"{C.GREEN}✅ {msg}{C.RESET}")


def warn(msg: str) -> None:
    print(f"{C.YELLOW}⚠️  {msg}{C.RESET}")


def fail(msg: str) -> None:
    print(f"{C.RED}❌ {msg}{C.RESET}", file=sys.stderr)


def _rule(color: str = C.DIM) -> None:
    print(f"{color}{'─' * 60}{C.RESET}")


def banner_start(name: str, help_text: str) -> None:
    """Announce a task is starting."""
    _rule(C.BLUE)
    print(f"{C.BOLD}{C.BLUE}▶  {name}{C.RESET} {C.DIM}— {help_text}{C.RESET}")
    _rule(C.BLUE)


def banner_end(name: str, elapsed: float, code: int) -> None:
    """Report whether a task finished cleanly, with how long it took."""
    secs = f"{elapsed:.1f}s"
    if code == 0:
        print(
            f"{C.GREEN}{C.BOLD}✅ {name} succeeded{C.RESET} "
            f"{C.DIM}({secs}){C.RESET}"
        )
    else:
        print(
            f"{C.RED}{C.BOLD}❌ {name} failed{C.RESET} "
            f"{C.DIM}(exit {code}, {secs}){C.RESET}",
            file=sys.stderr,
        )


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    """Echo a command (dimmed) and run it, inheriting stdio."""
    print(f"{C.DIM}$ {' '.join(cmd)}{C.RESET}")
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if check and proc.returncode != 0:
        fail(f"command failed (exit {proc.returncode}): {' '.join(cmd)}")
        sys.exit(proc.returncode)
    return proc.returncode


def cargo(*args: str, **kwargs) -> int:
    """Run cargo from the workspace root (where the lockfile lives)."""
    return run(["cargo", *args], cwd=WORKSPACE, **kwargs)


def load_dotenv() -> dict[str, str]:
    """Merge .env (if present) over the current environment for child processes."""
    env = dict(os.environ)
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # strip inline comments and surrounding quotes
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def require(tool: str, hint: str) -> None:
    if shutil.which(tool) is None:
        fail(f"`{tool}` not found. {hint}")
        sys.exit(1)


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if a TCP connection to host:port succeeds — i.e. something is serving."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def redis_host_port(url: str) -> tuple[str, int]:
    """Pull host/port out of a REDIS_URL like `redis://[:pass@]host:port/db`."""
    rest = url.split("://", 1)[-1]
    if "@" in rest:  # drop any `user:pass@` credentials
        rest = rest.rsplit("@", 1)[-1]
    hostport = rest.split("/", 1)[0]
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        return (host or "localhost"), int(port or 6303)
    return (hostport or "localhost"), 6303


# name -> (func, emoji, group, help). Order of registration drives `help`.
TASKS: dict[str, tuple] = {}


def task(name: str, emoji: str, group: str, help: str):
    def deco(fn):
        TASKS[name] = (fn, emoji, group, help)
        return fn

    return deco


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


@task("setup", "🛠️", "Setup", "Copy .env.example → .env (skips if .env exists)")
def setup() -> None:
    if ENV_FILE.exists():
        warn(".env already exists — not overwriting")
        return
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    ok("created .env from .env.example")


# --------------------------------------------------------------------------- #
# Docker / services
# --------------------------------------------------------------------------- #


@task("up", "🐳", "Services", "Start Redis (the cross-node bus, only needed for V4)")
def up() -> None:
    ensure_redis()


def ensure_redis() -> None:
    """Use whatever Redis is already serving REDIS_URL (host install or a
    container started elsewhere); only spin up the Compose `redis` service when
    nothing answers. Avoids the `bind: address already in use` clash when a host
    redis-server is running."""
    host, port = redis_host_port(
        load_dotenv().get("REDIS_URL", "redis://localhost:6303/0")
    )
    if port_open(host, port):
        ok(f"Redis already reachable at {host}:{port} — using it")
        return
    step("🐳", "no Redis on that port — starting the Compose redis service…")
    run([*COMPOSE, "up", "-d", "redis"], cwd=PROJECT_DIR)
    wait_redis()


@task("deps", "🐳", "Services", "Alias for `up`")
def deps() -> None:
    up()


@task("down", "🛑", "Services", "Stop docker services")
def down() -> None:
    step("🛑", "stopping services…")
    run([*COMPOSE, "down"], cwd=PROJECT_DIR)
    ok("services stopped")


@task("ps", "📋", "Services", "Show docker service status")
def ps() -> None:
    run([*COMPOSE, "ps"], cwd=PROJECT_DIR)


@task("logs", "📜", "Services", "Follow docker logs")
def logs() -> None:
    run([*COMPOSE, "logs", "-f"], cwd=PROJECT_DIR, check=False)


@task("wait-redis", "⏳", "Services", "Block until Redis answers PING")
def wait_redis() -> None:
    step("⏳", "waiting for Redis…")
    for _ in range(30):
        probe = subprocess.run(
            [*COMPOSE, "exec", "-T", "redis", "redis-cli", "ping"],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            ok("Redis is ready")
            return
        time.sleep(1)
    fail("Redis did not become ready in time")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Build / checks
# --------------------------------------------------------------------------- #


@task("check", "🔎", "Checks", "cargo check this crate")
def check() -> None:
    cargo("check", "-p", CRATE)


@task("clippy", "📎", "Checks", "cargo clippy with warnings denied")
def clippy() -> None:
    cargo("clippy", "-p", CRATE, "--", "-D", "warnings")


@task("fmt", "🎨", "Checks", "Format workspace Rust code")
def fmt() -> None:
    cargo("fmt", "--all")
    ok("formatted")


@task("fmt-check", "🎨", "Checks", "Fail if code is not formatted")
def fmt_check() -> None:
    cargo("fmt", "--all", "--", "--check")


@task("test", "🧪", "Checks", "Run crate tests")
def test() -> None:
    cargo("test", "-p", CRATE)


@task("verify", "✔️", "Checks", "Run all static checks + tests")
def verify() -> None:
    step("✔️", "running fmt-check → clippy → check → test")
    fmt_check()
    clippy()
    check()
    test()
    ok("verify: OK")


@task("clean", "🧹", "Checks", "cargo clean for this crate")
def clean() -> None:
    cargo("clean", "-p", CRATE)
    ok("cleaned")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


@task("run", "🚀", "Run", "Run the server (loads .env)")
def run_server() -> None:
    setup()
    step("🚀", f"starting {CRATE}…")
    cargo("run", "-p", CRATE, env=load_dotenv())


@task("dev", "🚀", "Run", "Start deps (Redis), then run the server")
def dev() -> None:
    deps()
    run_server()


@task("smoke", "🔥", "Run", "Hit /healthz (server must be running)")
def smoke() -> None:
    require("curl", "Install curl to use this target.")
    port = load_dotenv().get("PORT", "8080")
    step("🔥", f"GET http://localhost:{port}/healthz")
    rc = run(["curl", "-sf", f"http://localhost:{port}/healthz"], check=False)
    print()
    if rc == 0:
        ok("healthz OK")
    else:
        fail("healthz failed — is the server running?")
        sys.exit(1)


@task("help", "❓", "Meta", "Show this help")
def help_() -> None:
    print_project_help(
        title="📡 realtime-pubsub",
        tasks=TASKS,
        footers=[
            (
                "Typical first run",
                "make setup && make run   (single node; add deps for V4)",
            ),
            ("Multi-node (V4)", "CLUSTER=true, run two nodes on different PORTs"),
            ("Run all checks", "make verify"),
        ],
    )


def run_task(name: str, entry: tuple) -> int:
    """Run one top-level task wrapped in start/finish banners + timing."""
    fn, _, _, help_text = entry
    if name == "help":
        fn()
        return 0

    banner_start(name, help_text)
    start = time.perf_counter()
    try:
        fn()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        banner_end(name, time.perf_counter() - start, code)
        return code
    except Exception as exc:  # unexpected error in the task itself
        fail(str(exc))
        banner_end(name, time.perf_counter() - start, 1)
        return 1
    banner_end(name, time.perf_counter() - start, 0)
    return 0


def main(argv: list[str]) -> int:
    targets = argv or ["help"]
    for name in targets:
        entry = TASKS.get(name)
        if entry is None:
            fail(f"unknown task: {name}")
            print(f"{C.DIM}Run `make help` to list tasks.{C.RESET}")
            return 2
        code = run_task(name, entry)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(line_buffering=True)
        except ValueError:
            pass
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        warn("interrupted")
        sys.exit(130)
