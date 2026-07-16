"""Shared infrastructure for per-project ``makefile.py`` task runners.

Each project keeps a thin ``makefile.py`` that constructs a :class:`ProjectRunner`,
registers common task bundles, and adds project-specific ``@runner.task`` handlers.
Help tables remain in :mod:`makefile_help`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TaskEntry = tuple[Callable[[], None], str, str, str]

# Frontend folder names auto-detected by `discover_dev_panes` / `register_dev_stack`
# (same convention as tools/dev.py).
FRONTEND_DIRS = ("web", "dashboard", "ui", "frontend")


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


@dataclass
class ProjectConfig:
    crate: str
    help_title: str
    project_dir: Path
    workspace: Path
    help_footers: Sequence[tuple[str, str]] = field(default_factory=list)
    default_port: str = "8080"
    compose: list[str] = field(default_factory=lambda: ["docker", "compose"])

    @property
    def env_file(self) -> Path:
        return self.project_dir / ".env"

    @property
    def env_example(self) -> Path:
        return self.project_dir / ".env.example"


class ProjectRunner:
    """Per-project task runner: registry, console helpers, and dispatch."""

    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.tasks: dict[str, TaskEntry] = {}

    @property
    def project_dir(self) -> Path:
        return self.config.project_dir

    @property
    def workspace(self) -> Path:
        return self.config.workspace

    @property
    def compose(self) -> list[str]:
        return self.config.compose

    @property
    def crate(self) -> str:
        return self.config.crate

    def step(self, emoji: str, msg: str) -> None:
        print(f"{C.CYAN}{emoji}  {msg}{C.RESET}")

    def ok(self, msg: str) -> None:
        print(f"{C.GREEN}✅ {msg}{C.RESET}")

    def warn(self, msg: str) -> None:
        print(f"{C.YELLOW}⚠️  {msg}{C.RESET}")

    def fail(self, msg: str) -> None:
        print(f"{C.RED}❌ {msg}{C.RESET}", file=sys.stderr)

    def rule(self, color: str = C.DIM) -> None:
        print(f"{color}{'─' * 60}{C.RESET}")

    def banner_start(self, name: str, help_text: str) -> None:
        self.rule(C.BLUE)
        print(f"{C.BOLD}{C.BLUE}▶  {name}{C.RESET} {C.DIM}— {help_text}{C.RESET}")
        self.rule(C.BLUE)

    def banner_end(self, name: str, elapsed: float, code: int) -> None:
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
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> int:
        print(f"{C.DIM}$ {' '.join(cmd)}{C.RESET}")
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
        if check and proc.returncode != 0:
            self.fail(f"command failed (exit {proc.returncode}): {' '.join(cmd)}")
            sys.exit(proc.returncode)
        return proc.returncode

    def cargo(self, *args: str, **kwargs: Any) -> int:
        return self.run(["cargo", *args], cwd=self.workspace, **kwargs)

    def load_dotenv(self) -> dict[str, str]:
        env = dict(os.environ)
        env_file = self.config.env_file
        if not env_file.exists():
            return env
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.split("#", 1)[0].strip().strip('"').strip("'")
            env[key.strip()] = value
        return env

    def require(self, tool: str, hint: str) -> None:
        if shutil.which(tool) is None:
            self.fail(f"`{tool}` not found. {hint}")
            sys.exit(1)

    def port_open(self, host: str, port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def redis_host_port(self, url: str, default_port: int) -> tuple[str, int]:
        rest = url.split("://", 1)[-1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[-1]
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            host, _, port = hostport.rpartition(":")
            return (host or "localhost"), int(port or default_port)
        return (hostport or "localhost"), default_port

    def task(self, name: str, emoji: str, group: str, help: str) -> Callable:
        def deco(fn: Callable[[], None]) -> Callable[[], None]:
            self.tasks[name] = (fn, emoji, group, help)
            return fn

        return deco

    def run_task(self, name: str, entry: TaskEntry) -> int:
        fn, _, _, help_text = entry
        if name == "help":
            fn()
            return 0

        self.banner_start(name, help_text)
        start = time.perf_counter()
        try:
            fn()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            self.banner_end(name, time.perf_counter() - start, code)
            return code
        except Exception as exc:
            self.fail(str(exc))
            self.banner_end(name, time.perf_counter() - start, 1)
            return 1
        self.banner_end(name, time.perf_counter() - start, 0)
        return 0

    def main(self, argv: list[str] | None = None) -> int:
        targets = argv if argv is not None else ["help"]
        if not targets:
            targets = ["help"]
        for name in targets:
            entry = self.tasks.get(name)
            if entry is None:
                self.fail(f"unknown task: {name}")
                print(f"{C.DIM}Run `make help` to list tasks.{C.RESET}")
                return 2
            code = self.run_task(name, entry)
            if code != 0:
                return code
        return 0

    def entrypoint(self, argv: list[str] | None = None) -> None:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(line_buffering=True)
            except ValueError:
                pass
        try:
            sys.exit(self.main(argv))
        except KeyboardInterrupt:
            print()
            self.warn("interrupted")
            sys.exit(130)


def bootstrap_tools_path(project_dir: Path) -> Path:
    """Insert ``tools/`` on ``sys.path`` and return the workspace root."""
    workspace = project_dir.parent.parent
    tools = str(workspace / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return workspace


def make_runner(
    *,
    crate: str,
    help_title: str,
    project_dir: Path | None = None,
    help_footers: Sequence[tuple[str, str]] = (),
    default_port: str = "8080",
    compose: list[str] | None = None,
) -> ProjectRunner:
    """Construct a :class:`ProjectRunner` with paths derived from the caller."""
    if project_dir is None:
        import inspect

        frame = inspect.stack()[1]
        project_dir = Path(frame.filename).resolve().parent
    workspace = bootstrap_tools_path(project_dir)
    config = ProjectConfig(
        crate=crate,
        help_title=help_title,
        project_dir=project_dir,
        workspace=workspace,
        help_footers=help_footers,
        default_port=default_port,
        compose=compose or ["docker", "compose"],
    )
    return ProjectRunner(config)


# --------------------------------------------------------------------------- #
# Task bundles
# --------------------------------------------------------------------------- #


def register_setup(runner: ProjectRunner) -> Callable[[], None]:
    @runner.task(
        "setup", "🛠️", "Setup", "Copy .env.example → .env (skips if .env exists)"
    )
    def setup() -> None:
        env_file = runner.config.env_file
        env_example = runner.config.env_example
        if env_file.exists():
            runner.warn(".env already exists — not overwriting")
            return
        shutil.copyfile(env_example, env_file)
        runner.ok("created .env from .env.example")

    return setup


def register_cargo_checks(runner: ProjectRunner) -> dict[str, Callable[[], None]]:
    crate = runner.crate

    @runner.task("check", "🔎", "Checks", "cargo check this crate")
    def check() -> None:
        runner.cargo("check", "-p", crate)

    @runner.task("clippy", "📎", "Checks", "cargo clippy with warnings denied")
    def clippy() -> None:
        runner.cargo("clippy", "-p", crate, "--", "-D", "warnings")

    @runner.task("fmt", "🎨", "Checks", "Format workspace Rust code")
    def fmt() -> None:
        runner.cargo("fmt", "--all")
        runner.ok("formatted")

    @runner.task("fmt-check", "🎨", "Checks", "Fail if code is not formatted")
    def fmt_check() -> None:
        runner.cargo("fmt", "--all", "--", "--check")

    @runner.task("test", "🧪", "Checks", "Run crate tests")
    def test() -> None:
        runner.cargo("test", "-p", crate)

    @runner.task("verify", "✔️", "Checks", "Run all static checks + tests")
    def verify() -> None:
        runner.step("✔️", "running fmt-check → clippy → check → test")
        fmt_check()
        clippy()
        check()
        test()
        runner.ok("verify: OK")

    @runner.task("clean", "🧹", "Checks", "cargo clean for this crate")
    def clean() -> None:
        runner.cargo("clean", "-p", crate)
        runner.ok("cleaned")

    return {
        "check": check,
        "clippy": clippy,
        "fmt": fmt,
        "fmt_check": fmt_check,
        "test": test,
        "verify": verify,
        "clean": clean,
    }


def register_compose_lifecycle(runner: ProjectRunner) -> dict[str, Callable[[], None]]:
    @runner.task("down", "🛑", "Services", "Stop docker services")
    def down() -> None:
        runner.step("🛑", "stopping services…")
        runner.run([*runner.compose, "down"], cwd=runner.project_dir)
        runner.ok("services stopped")

    @runner.task("ps", "📋", "Services", "Show docker service status")
    def ps() -> None:
        runner.run([*runner.compose, "ps"], cwd=runner.project_dir)

    @runner.task("logs", "📜", "Services", "Follow docker logs")
    def logs() -> None:
        runner.run([*runner.compose, "logs", "-f"], cwd=runner.project_dir, check=False)

    return {"down": down, "ps": ps, "logs": logs}


def register_postgres(
    runner: ProjectRunner,
    *,
    user: str,
    include_install_tools: bool = True,
    include_prepare: bool = True,
) -> dict[str, Callable[[], None]]:
    def _setup() -> None:
        entry = runner.tasks.get("setup")
        if entry is not None:
            entry[0]()

    if include_install_tools:

        @runner.task(
            "install-tools", "📦", "Setup", "Install sqlx-cli for migrations (Postgres)"
        )
        def install_tools() -> None:
            runner.step("📦", "installing sqlx-cli (rustls + postgres)…")
            runner.run(
                [
                    "cargo",
                    "install",
                    "sqlx-cli",
                    "--no-default-features",
                    "--features",
                    "rustls,postgres",
                ]
            )
            runner.ok("sqlx-cli installed")

    else:
        install_tools = lambda: None  # type: ignore[assignment,return-value]

    @runner.task(
        "wait-db", "⏳", "Services", "Block until Postgres accepts connections"
    )
    def wait_db() -> None:
        runner.step("⏳", "waiting for Postgres…")
        for _ in range(30):
            probe = subprocess.run(
                [
                    *runner.compose,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_isready",
                    "-U",
                    user,
                ],
                cwd=str(runner.project_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                runner.ok("Postgres is ready")
                return
            time.sleep(1)
        runner.fail("Postgres did not become ready in time")
        sys.exit(1)

    @runner.task("migrate", "🗃️", "Services", "Apply SQL migrations (needs sqlx-cli)")
    def migrate() -> None:
        _setup()
        runner.require("sqlx", "Run: make install-tools")
        runner.step("🗃️", "applying migrations…")
        runner.run(
            ["sqlx", "migrate", "run"],
            cwd=runner.project_dir,
            env=runner.load_dotenv(),
        )
        runner.ok("migrations applied")

    if include_prepare:

        @runner.task(
            "prepare",
            "🧬",
            "Services",
            "Regenerate sqlx offline query cache (needs Postgres + migrations)",
        )
        def prepare() -> None:
            runner.require("sqlx", "Run: make install-tools")
            migrate()
            runner.step("🧬", "preparing sqlx query cache…")
            runner.run(
                ["cargo", "sqlx", "prepare", "--", "--all-targets"],
                cwd=runner.project_dir,
                env=runner.load_dotenv(),
            )
            runner.ok("sqlx cache updated — commit .sqlx/")

    else:
        prepare = lambda: None  # type: ignore[assignment,return-value]

    @runner.task(
        "reset-db", "💥", "Services", "Drop volumes and recreate DB (destructive)"
    )
    def reset_db() -> None:
        runner.warn("dropping volumes — this wipes the database")
        runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir)
        runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
        wait_db()
        migrate()

    return {
        "wait_db": wait_db,
        "migrate": migrate,
        "prepare": prepare,
        "reset_db": reset_db,
    }


def register_redis(
    runner: ProjectRunner,
    *,
    default_port: int,
    redis_url_key: str = "REDIS_URL",
    default_redis_url: str | None = None,
    include_reset: bool = False,
) -> dict[str, Callable[[], None]]:
    if default_redis_url is None:
        default_redis_url = f"redis://localhost:{default_port}/0"

    @runner.task("wait-redis", "⏳", "Services", "Block until Redis answers PING")
    def wait_redis() -> None:
        runner.step("⏳", "waiting for Redis…")
        for _ in range(30):
            probe = subprocess.run(
                [*runner.compose, "exec", "-T", "redis", "redis-cli", "ping"],
                cwd=str(runner.project_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                runner.ok("Redis is ready")
                return
            time.sleep(1)
        runner.fail("Redis did not become ready in time")
        sys.exit(1)

    def ensure_redis() -> None:
        """Use existing Redis when reachable; otherwise start the Compose service."""
        host, port = runner.redis_host_port(
            runner.load_dotenv().get(redis_url_key, default_redis_url),
            default_port,
        )
        if runner.port_open(host, port):
            runner.ok(f"Redis already reachable at {host}:{port} — using it")
            return
        runner.step("🐳", "no Redis on that port — starting the Compose redis service…")
        runner.run([*runner.compose, "up", "-d", "redis"], cwd=runner.project_dir)
        wait_redis()

    result: dict[str, Callable[[], None]] = {
        "wait_redis": wait_redis,
        "ensure_redis": ensure_redis,
    }

    if include_reset:

        @runner.task(
            "reset", "💥", "Services", "Drop volumes and recreate Redis (wipes state)"
        )
        def reset_redis() -> None:
            runner.warn("dropping volumes — this wipes all limiter state in Redis")
            runner.run([*runner.compose, "down", "-v"], cwd=runner.project_dir)
            runner.run([*runner.compose, "up", "-d"], cwd=runner.project_dir)
            wait_redis()

        result["reset_redis"] = reset_redis

    return result


def register_run(runner: ProjectRunner) -> Callable[[], None]:
    setup = runner.tasks.get("setup")
    setup_fn = setup[0] if setup else lambda: None

    @runner.task("run", "🚀", "Run", "Run the server (loads .env)")
    def run_server() -> None:
        setup_fn()
        runner.step("🚀", f"starting {runner.crate}…")
        runner.cargo("run", "-p", runner.crate, env=runner.load_dotenv())

    return run_server


def register_smoke_healthz(runner: ProjectRunner) -> Callable[[], None]:
    @runner.task("smoke", "🔥", "Run", "Hit /healthz (server must be running)")
    def smoke() -> None:
        runner.require("curl", "Install curl to use this target.")
        port = runner.load_dotenv().get("PORT", runner.config.default_port)
        runner.step("🔥", f"GET http://localhost:{port}/healthz")
        rc = runner.run(
            ["curl", "-sf", f"http://localhost:{port}/healthz"],
            check=False,
        )
        print()
        if rc == 0:
            runner.ok("healthz OK")
        else:
            runner.fail("healthz failed — is the server running?")
            sys.exit(1)

    return smoke


# --------------------------------------------------------------------------- #
# Auto-detected full-stack `make dev` (compose + server + frontend)
# --------------------------------------------------------------------------- #


def find_compose_file(proj: Path) -> Path | None:
    for name in ("docker-compose.yml", "compose.yml"):
        if (proj / name).exists():
            return proj / name
    return None


def find_frontends(proj: Path) -> list[Path]:
    return [proj / d for d in FRONTEND_DIRS if (proj / d / "package.json").exists()]


def discover_dev_panes(
    proj: Path,
    *,
    prefix: str = "",
    use_cargo_watch: bool = True,
) -> dict[str, dict[str, str]]:
    """Pane name → mprocs proc entry, derived from what the project has on disk.

    Same rules as ``tools/dev.py``:

    * compose file → ``deps`` pane (``docker compose up``)
    * ``src/main.rs`` → ``server`` (compose up -d --wait, optional migrate, cargo)
    * ``web|dashboard|ui|frontend/package.json`` → Bun Vite pane per dir
    """
    out: dict[str, dict[str, str]] = {}
    compose = find_compose_file(proj)

    if compose is not None:
        out[f"{prefix}deps"] = {"shell": "docker compose up", "cwd": str(proj)}

    if (proj / "src" / "main.rs").exists():
        steps: list[str] = []
        if compose is not None:
            steps.append("docker compose up -d --wait")
        if (proj / "migrations").is_dir():
            steps.append("[ -f .env ] && sqlx migrate run")
        if use_cargo_watch:
            steps.append("exec cargo watch -q -x run")
        else:
            steps.append("exec cargo run")
        out[f"{prefix}server"] = {"shell": "; ".join(steps), "cwd": str(proj)}

    for fe in find_frontends(proj):
        out[f"{prefix}{fe.name}"] = {
            "shell": "[ -d node_modules ] || bun install; exec bun run dev",
            "cwd": str(fe),
        }
    return out


def launch_mprocs(procs: dict[str, dict[str, str]]) -> None:
    """Write a temp mprocs config and exec into mprocs (does not return)."""
    if shutil.which("mprocs") is None:
        print(
            f"{C.RED}❌ `mprocs` not found — install with: cargo install mprocs{C.RESET}",
            file=sys.stderr,
        )
        sys.exit(1)
    path = Path(tempfile.mkdtemp(prefix="gauntlet-dev-")) / "mprocs.yaml"
    # JSON is valid YAML, so no PyYAML dependency.
    path.write_text(json.dumps({"procs": procs}, indent=2))
    os.execvp("mprocs", ["mprocs", "--config", str(path)])


def register_dev_stack(
    runner: ProjectRunner,
    *,
    use_cargo_watch: bool = True,
    vite_port: str = "5173",
) -> dict[str, Callable[[], None]]:
    """Register ``dev`` (+ ``frontend`` / ``web-install`` when a UI exists).

    Discovers Docker Compose, the Rust server, and Bun frontends under the
    project dir — same auto-detection as root ``make dev NN=…`` — so a
    per-project ``make dev`` launches the full local stack in one mprocs session.
    """
    proj = runner.project_dir
    fes = find_frontends(proj)

    def _panes() -> dict[str, dict[str, str]]:
        return discover_dev_panes(proj, use_cargo_watch=use_cargo_watch)

    @runner.task(
        "dev",
        "🚀",
        "Run",
        "Full stack: auto-detect deps + server + frontend (mprocs)",
    )
    def dev() -> None:
        panes = _panes()
        if not panes:
            runner.fail(
                "nothing to run (no compose file, src/main.rs, or frontend found)"
            )
            sys.exit(1)

        if use_cargo_watch and "server" in panes:
            runner.require(
                "cargo-watch",
                "Install with: cargo install cargo-watch",
            )
        if any(name in FRONTEND_DIRS for name in panes):
            runner.require(
                "bun",
                "Install Bun: https://bun.sh  (curl -fsSL https://bun.sh/install | bash)",
            )

        runner.step("🚀", f"launching mprocs panes: {', '.join(panes)}")
        for name in panes:
            if name in FRONTEND_DIRS:
                print(
                    f"   {C.BOLD}{C.CYAN}http://localhost:{vite_port}{C.RESET} "
                    f"{C.DIM}← open the {name} playground once Vite is ready{C.RESET}"
                )
        port = runner.load_dotenv().get("PORT", runner.config.default_port)
        if "server" in panes:
            print(
                f"   {C.DIM}backend http://localhost:{port} "
                f"(cargo {'watch' if use_cargo_watch else 'run'}){C.RESET}"
            )
        print(f"   {C.DIM}Ctrl-C inside mprocs stops the stack.{C.RESET}")
        launch_mprocs(panes)

    result: dict[str, Callable[[], None]] = {"dev": dev}

    if fes:

        def _ensure_web_deps(fe: Path) -> None:
            runner.require(
                "bun",
                "Install Bun: https://bun.sh  (curl -fsSL https://bun.sh/install | bash)",
            )
            if (fe / "node_modules").is_dir():
                return
            runner.step("📦", f"installing {fe.name} deps (bun install)…")
            runner.run(["bun", "install"], cwd=fe)

        @runner.task(
            "web-install",
            "📦",
            "Setup",
            "Install frontend deps (bun install) for detected UI dirs",
        )
        def web_install() -> None:
            for fe in fes:
                runner.step("📦", f"bun install in {fe.name}/…")
                runner.run(["bun", "install"], cwd=fe)
            runner.ok("frontend deps installed")

        @runner.task(
            "frontend",
            "🌐",
            "Run",
            f"Run just the web playground (Vite, :{vite_port})",
        )
        def frontend() -> None:
            fe = fes[0]
            _ensure_web_deps(fe)
            runner.step(
                "🌐",
                f"starting {fe.name}/ on http://localhost:{vite_port} "
                f"(backend must already be running)…",
            )
            runner.run(["bun", "run", "dev"], cwd=fe, check=False)

        result["web_install"] = web_install
        result["frontend"] = frontend

    return result


def register_help(runner: ProjectRunner) -> Callable[[], None]:
    from makefile_help import print_project_help

    @runner.task("help", "❓", "Meta", "Show this help")
    def help_() -> None:
        print_project_help(
            title=runner.config.help_title,
            tasks=runner.tasks,
            footers=runner.config.help_footers,
        )

    return help_
