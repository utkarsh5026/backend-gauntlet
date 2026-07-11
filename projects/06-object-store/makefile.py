#!/usr/bin/env python3
"""object-store — local dev task runner.

A small, dependency-free wrapper around the day-to-day commands for this crate
(cargo, the Bun/Vite web console, curl smoke checks). The `Makefile` shells out
to this file so you get one source of truth with colors, emojis and readable
output.

Unlike the DB-backed projects there is **no Docker** here: the filesystem *is*
the database (objects/, index/, tmp/, uploads/ under DATA_DIR). So the headline
task is `dev` — it launches the Rust backend **and** the web console together,
wired to the same port, so you can watch uploads/listing/multipart in action.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR.parent.parent
WEB_DIR = PROJECT_DIR / "web"
CRATE = "object-store"
ENV_FILE = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"

# The web console proxies /s3 → the backend and defaults to :9006 (project 06);
# :9000 is skipped because MinIO squats it. Keep backend + proxy on this port.
DEFAULT_PORT = "9006"
WEB_PORT = "5173"  # Vite dev server (see web/vite.config.ts)


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


def backend_port() -> int:
    """The port the store binds to: PORT from .env, else the project-06 default."""
    return int(load_dotenv().get("PORT", DEFAULT_PORT))


def backend_env(port: int) -> dict[str, str]:
    """Env for `cargo run`: force PORT and keep DATA_DIR under the project dir so
    the on-disk store lives next to the crate no matter where cargo is invoked."""
    env = load_dotenv()
    env["PORT"] = str(port)
    env.setdefault("DATA_DIR", str(PROJECT_DIR / "data"))
    return env


def ensure_web_deps() -> None:
    """Install the console's node_modules on first run (idempotent)."""
    require("bun", "Install Bun: https://bun.sh  (curl -fsSL https://bun.sh/install | bash)")
    if (WEB_DIR / "node_modules").is_dir():
        return
    step("📦", "installing web console deps (bun install)…")
    run(["bun", "install"], cwd=WEB_DIR)


def wait_backend(port: int, proc: subprocess.Popen, tries: int = 60) -> bool:
    """Poll until the backend accepts connections, or it exits first."""
    step("⏳", f"waiting for the backend on :{port}…")
    for _ in range(tries):
        if proc.poll() is not None:  # cargo/backend died before it bound the port
            return False
        if port_open("localhost", port):
            return True
        time.sleep(0.5)
    return False


def _popen_session(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """Popen in its own process group (Unix) so `dev` can killpg the whole subtree
    — cargo→binary, bun→vite→esbuild — cleanly on exit regardless of how the
    signal arrives. Children inherit stdio so their logs still stream to you."""
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)


def spawn_backend(port: int) -> subprocess.Popen:
    """Start `cargo run -p object-store` on `port` in its own process group."""
    print(f"{C.DIM}$ PORT={port} cargo run -p {CRATE}{C.RESET}")
    return _popen_session(["cargo", "run", "-p", CRATE], PROJECT_DIR, backend_env(port))


def spawn_web(port: int) -> subprocess.Popen:
    """Start the Vite dev server in its own process group, proxying /s3 → the
    backend on `port`."""
    env = dict(os.environ)
    env["OBJECT_STORE_URL"] = f"http://localhost:{port}"
    print(f"{C.DIM}$ OBJECT_STORE_URL=http://localhost:{port} bun run dev{C.RESET}")
    return _popen_session(["bun", "run", "dev"], WEB_DIR, env)


def stop_proc(proc: subprocess.Popen, label: str) -> None:
    """Signal a process's whole group and wait for it to drain (SIGTERM, then
    SIGKILL if it lingers)."""
    if proc.poll() is not None:
        return
    step("🛑", f"stopping {label}…")
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()


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


@task("web-install", "📦", "Setup", "Install web console deps (bun install)")
def web_install() -> None:
    require("bun", "Install Bun: https://bun.sh")
    step("📦", "installing web console deps…")
    run(["bun", "install"], cwd=WEB_DIR)
    ok("web deps installed")


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


@task(
    "dev",
    "🚀",
    "Run",
    "Launch backend + web console together (the full demo) — Ctrl-C stops both",
)
def dev() -> None:
    require("cargo", "Install Rust: https://rustup.rs")
    ensure_web_deps()
    port = backend_port()

    backend = spawn_backend(port)
    web: subprocess.Popen | None = None
    try:
        if not wait_backend(port, backend):
            fail("backend did not come up (it exited or never bound the port)")
            sys.exit(1)
        ok(f"backend is serving http://localhost:{port}")

        _rule(C.MAGENTA)
        print(f"{C.BOLD}{C.MAGENTA}🎬  Object-store console is coming up{C.RESET}")
        print(
            f"   Console:  {C.BOLD}{C.CYAN}http://localhost:{WEB_PORT}{C.RESET} "
            f"{C.DIM}(open this){C.RESET}"
        )
        print(f"   Backend:  {C.DIM}http://localhost:{port}  (S3 path-style API){C.RESET}")
        print(
            f"   {C.DIM}Create a bucket → PUT an object → watch it list. "
            f"Multipart tab shows the -N ETag.{C.RESET}"
        )
        print(f"   {C.DIM}Ctrl-C stops both.{C.RESET}")
        _rule(C.MAGENTA)

        # Frontend runs in its own group and streams its logs here; wait on it so
        # Ctrl-C (or the console exiting) drops us into `finally`, which tears down
        # BOTH subtrees — no orphaned vite/esbuild or backend left behind.
        web = spawn_web(port)
        try:
            web.wait()
        except KeyboardInterrupt:
            print()
            warn("interrupted — shutting down")
    finally:
        if web is not None:
            stop_proc(web, "web console")
        stop_proc(backend, "backend")


@task("backend", "🦀", "Run", f"Run just the Rust store (PORT={DEFAULT_PORT}, loads .env)")
def backend() -> None:
    require("cargo", "Install Rust: https://rustup.rs")
    port = backend_port()
    step("🦀", f"starting {CRATE} on http://localhost:{port} …")
    run(["cargo", "run", "-p", CRATE], cwd=PROJECT_DIR, env=backend_env(port))


@task("frontend", "🌐", "Run", f"Run just the web console (Vite dev, :{WEB_PORT})")
def frontend() -> None:
    ensure_web_deps()
    port = backend_port()
    web_env = dict(os.environ)
    web_env["OBJECT_STORE_URL"] = f"http://localhost:{port}"
    step("🌐", f"starting the console on http://localhost:{WEB_PORT} "
               f"(proxying /s3 → :{port})…")
    run(["bun", "run", "dev"], cwd=WEB_DIR, env=web_env, check=False)


@task("web-build", "🏗️", "Run", "Production build of the web console (tsc + vite build)")
def web_build() -> None:
    require("bun", "Install Bun: https://bun.sh")
    ensure_web_deps()
    step("🏗️", "building the web console…")
    run(["bun", "run", "build"], cwd=WEB_DIR)
    ok(f"built → {WEB_DIR / 'dist'}")


@task("smoke", "🔥", "Run", "Hit /healthz on the backend (server must be running)")
def smoke() -> None:
    require("curl", "Install curl to use this target.")
    port = backend_port()
    url = f"http://localhost:{port}/healthz"
    step("🔥", f"GET {url}")
    rc = run(["curl", "-sf", url], check=False)
    print()
    if rc == 0:
        ok("healthz OK")
    else:
        fail("healthz failed — is the backend running? (make backend / make dev)")
        sys.exit(1)


@task("help", "❓", "Meta", "Show this help")
def help_() -> None:
    print()
    print(
        f"{C.BOLD}{C.MAGENTA}🗄️  object-store{C.RESET} {C.DIM}— common commands{C.RESET}\n"
    )

    groups: dict[str, list[str]] = {}
    for name, (_, _, group, _) in TASKS.items():
        groups.setdefault(group, []).append(name)

    width = max(len(n) for n in TASKS) + 2
    for group, names in groups.items():
        print(f"{C.BOLD}{C.YELLOW}{group}{C.RESET}")
        for name in names:
            _, emoji, _, help_text = TASKS[name]
            print(f"  {emoji}  {C.CYAN}{name:<{width}}{C.RESET}{help_text}")
        print()

    print(f"{C.BOLD}See it in action:{C.RESET} {C.DIM}make dev{C.RESET} "
          f"{C.DIM}(backend + console; open http://localhost:{WEB_PORT}){C.RESET}")
    print(f"{C.BOLD}Run all checks:{C.RESET}   {C.DIM}make verify{C.RESET}\n")


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
