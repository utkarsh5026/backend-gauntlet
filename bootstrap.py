#!/usr/bin/env python3
"""backend-gauntlet — one-command development environment bootstrap.

Turns a fresh clone into a working Python development environment on Linux,
macOS and Windows, without you having to read the README first::

    python bootstrap.py

That is idempotent: run it again after a ``git pull`` and it re-syncs whatever
moved. It performs, in order:

  1. **preflight** — interpreter, repo layout and write-permission checks
  2. **uv**        — locate uv, or install it from the official installer
  3. **python**    — ``uv python install <version>`` for the version uv.lock pins
  4. **venv**      — ``uv sync --all-packages --frozen`` → one root ``.venv``
  5. **env**       — seed each Python project's ``.env`` from its ``.env.example``
  6. **hooks**     — (opt-in) point core.hooksPath at .githooks
  7. **doctor**    — report the optional tools each project wants

Everything is stdlib-only and works on Python 3.8+, because it has to run
*before* the 3.13 workspace exists. It never overwrites an existing ``.env``,
never touches tracked files, and in ``--check`` mode writes nothing at all.

Usage::

    python bootstrap.py                 # full setup (safe to re-run)
    python bootstrap.py --check         # diagnose only — changes nothing
    python bootstrap.py --verify        # setup, then run the CI gate on every project
    python bootstrap.py --recreate      # delete .venv first (fixes a broken/foreign venv)
    python bootstrap.py --hooks         # also install the repo's git hooks
    python bootstrap.py --no-uv-install # never auto-install uv; just report it missing
    python bootstrap.py --quiet         # only warnings, errors and the summary

Exit codes: ``0`` success · ``1`` a step failed · ``2`` bad usage · ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import errno
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"
PACKAGES = ROOT / "packages"
VENV = ROOT / ".venv"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# This file must PARSE and RUN on whatever interpreter the user happens to have,
# since its whole job is installing the one the workspace actually wants. 3.8 is
# the floor for the syntax used here; the workspace floor is read from uv.lock.
MIN_BOOTSTRAP_PYTHON = (3, 8)
FALLBACK_WORKSPACE_PYTHON = "3.13"

UV_INSTALL_URL_UNIX = "https://astral.sh/uv/install.sh"
UV_INSTALL_URL_WINDOWS = "https://astral.sh/uv/install.ps1"

# Long-running network/compile steps get a ceiling so a hung proxy fails loudly
# instead of appearing to work forever.
TIMEOUT_QUICK = 60
TIMEOUT_INSTALL = 900


# ─────────────────────────────────────────────────────────────────────────────
# Console — colours and glyphs that survive a Windows console
# ─────────────────────────────────────────────────────────────────────────────


def _prepare_windows_console() -> None:
    """Make cmd.exe/PowerShell able to render UTF-8 and ANSI escapes.

    Two independent problems, both Windows-only: the legacy console defaults to
    a codepage (cp1252) that cannot encode ``✅``, and conhost does not process
    ANSI escapes unless ENABLE_VIRTUAL_TERMINAL_PROCESSING is set. Both fixes
    are best-effort — if either fails we fall back to ASCII glyphs / no colour
    rather than crashing on the very first line of output.
    """
    if not IS_WINDOWS:
        return

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        enable_vt = 0x0004
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | enable_vt)
    except Exception:  # noqa: BLE001 - console tuning is never worth failing over
        pass


def _console_handles_unicode() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✅ ⚠ ❌ → ·".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Style:
    """ANSI styles, off for pipes, ``NO_COLOR``, and consoles that can't do it."""

    _forced = os.environ.get("FORCE_COLOR", "").strip() not in ("", "0")
    _on = (sys.stdout.isatty() or _forced) and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    CYAN = "\033[36m" if _on else ""


GLYPHS_UNICODE = {
    "ok": "✅",
    "warn": "⚠️ ",
    "fail": "❌",
    "skip": "·",
    "step": "▶",
    "arrow": "→",
}
GLYPHS_ASCII = {
    "ok": "[ok]",
    "warn": "[!] ",
    "fail": "[x]",
    "skip": "-",
    "step": ">",
    "arrow": "->",
}


class Console:
    """Tiny printer: quiet mode, glyph fallback, and a consistent shape."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.glyphs = GLYPHS_UNICODE if _console_handles_unicode() else GLYPHS_ASCII
        self.warnings: list[str] = []

    def g(self, name: str) -> str:
        return self.glyphs[name]

    def say(self, msg: str = "") -> None:
        if not self.quiet:
            print(msg)

    def emit(self, msg: str = "") -> None:
        """Print regardless of --quiet. The summary is the point of the run."""
        print(msg)

    def step(self, msg: str) -> None:
        self.say(f"\n{Style.BOLD}{Style.BLUE}{self.g('step')} {msg}{Style.RESET}")

    def detail(self, msg: str) -> None:
        self.say(f"{Style.DIM}   {msg}{Style.RESET}")

    def command(self, argv: list[str]) -> None:
        self.say(f"{Style.DIM}   $ {shlex_join(argv)}{Style.RESET}")

    def ok(self, msg: str) -> None:
        self.say(f"   {Style.GREEN}{self.g('ok')} {msg}{Style.RESET}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"   {Style.YELLOW}{self.g('warn')} {msg}{Style.RESET}")

    def fail(self, msg: str) -> None:
        print(f"   {Style.RED}{self.g('fail')} {msg}{Style.RESET}", file=sys.stderr)

    def rule(self) -> None:
        self.say(f"{Style.DIM}{'─' * 72}{Style.RESET}")


def shlex_join(argv: list[str]) -> str:
    """A readable, copy-pasteable rendering of a command line, both platforms."""
    parts = []
    for arg in argv:
        parts.append(f'"{arg}"' if " " in arg else arg)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Failure model — every error carries the fix, not just the symptom
# ─────────────────────────────────────────────────────────────────────────────


class SetupError(Exception):
    """A step failed for a reason we can explain and usually tell you how to fix."""

    def __init__(
        self,
        message: str,
        remedies: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remedies = remedies or []
        self.detail = detail

    def render(self, console: Console) -> None:
        console.fail(self.message)
        if self.detail:
            for line in self.detail.strip().splitlines()[-12:]:
                print(f"{Style.DIM}      {line}{Style.RESET}", file=sys.stderr)
        for remedy in self.remedies:
            print(f"      {Style.CYAN}{console.g('arrow')} {remedy}{Style.RESET}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Permissions — the part that actually bites, and differently per platform
# ─────────────────────────────────────────────────────────────────────────────

# Windows system error codes we can give specific advice for. These arrive as
# OSError.winerror, which is unrelated to (and more precise than) errno.
WIN_ACCESS_DENIED = 5
WIN_SHARING_VIOLATION = 32
WIN_LOCK_VIOLATION = 33
WIN_PATH_TOO_LONG = 206
WIN_PRIVILEGE_NOT_HELD = 1314

SYNC_FOLDER_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")


def owner_description(path: Path) -> str:
    """``user:group`` owning *path* on POSIX; empty string elsewhere/unknown."""
    if IS_WINDOWS:
        return ""
    try:
        import grp
        import pwd

        info = path.stat()
        user = pwd.getpwuid(info.st_uid).pw_name
        group = grp.getgrgid(info.st_gid).gr_name
    except (OSError, KeyError, ImportError):
        return ""
    return f"{user}:{group}"


def permission_remedies(exc: OSError, path: Path) -> list[str]:
    """Translate a raw OSError on *path* into concrete, platform-correct fixes."""
    winerror = getattr(exc, "winerror", None)
    code = getattr(exc, "errno", None)

    if IS_WINDOWS:
        if winerror in (WIN_SHARING_VIOLATION, WIN_LOCK_VIOLATION):
            return [
                f"Something is holding {path} open. Close any running server "
                "(uvicorn), pyright/pylance language server, or editor tab using it.",
                "In VS Code: reload the window (Ctrl+Shift+P > Developer: Reload Window).",
                "Then re-run:  python bootstrap.py --recreate",
            ]
        if winerror == WIN_ACCESS_DENIED:
            return [
                f"Access denied on {path}.",
                "Close editors/terminals in this folder, then re-run in a NEW shell.",
                "If your antivirus (Defender, CrowdStrike, ...) scans this folder, "
                "add an exclusion for it — real-time scanning locks freshly written files.",
                "Do NOT run this as Administrator: an elevated .venv is unusable "
                "from your normal shell afterwards.",
            ]
        if winerror == WIN_PRIVILEGE_NOT_HELD:
            return [
                "Windows refused a symlink/hardlink. Either enable Developer Mode "
                "(Settings > System > For developers), or force uv to copy instead:",
                "    set UV_LINK_MODE=copy   (PowerShell: $env:UV_LINK_MODE='copy')",
            ]
        if winerror == WIN_PATH_TOO_LONG or (code == errno.ENOENT and len(str(path)) > 250):
            return [
                f"The path is too long ({len(str(path))} chars) for the Win32 API.",
                "Enable long paths (admin PowerShell, once):",
                "    New-ItemProperty -PropertyType DWORD -Force -Name LongPathsEnabled "
                "-Value 1 -Path "
                "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem'",
                "Or move the clone somewhere shallow, e.g. C:\\src\\backend-gauntlet.",
            ]
        return [
            f"Could not write {path}.",
            "Check the folder is not read-only and that no process has it open.",
        ]

    owner = owner_description(path)
    remedies = []
    if code == errno.EROFS:
        remedies.append(f"{path} is on a read-only filesystem — clone somewhere writable.")
        return remedies
    if code == errno.ENOSPC:
        remedies.append("The disk is full. Free space (a stale ./target or .venv is a good start).")
        return remedies
    remedies.append(f"Fix ownership/permissions on {path}.")
    if owner:
        remedies.append(f"It is owned by {owner}; you are running as uid {os.getuid()}.")
        if owner.startswith("root:") and os.getuid() != 0:
            remedies.append(f"Most likely a previous `sudo` run. Try:  sudo rm -rf {path}")
    remedies.append(f"Or:  chmod -R u+rwX {path}")
    return remedies


def clear_readonly(path: Path) -> bool:
    """Drop the read-only bit so a retry can succeed. True if we changed anything."""
    try:
        mode = path.stat().st_mode
        if not mode & stat.S_IWRITE:
            path.chmod(mode | stat.S_IWRITE)
            return True
    except OSError:
        return False
    return False


def probe_writable(directory: Path) -> None:
    """Prove *directory* is writable by actually writing to it.

    ``os.access(..., os.W_OK)`` lies on Windows (it only reports the read-only
    attribute, not ACLs) and lies under containers/ACLs on POSIX too. The only
    honest test is the write itself, so we do that and clean up.
    """
    probe = directory / ".bootstrap-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        raise SetupError(
            f"No write permission in {directory}",
            remedies=permission_remedies(exc, directory),
            detail=str(exc),
        ) from exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def rmtree_robust(path: Path) -> None:
    """``shutil.rmtree`` that survives read-only files (the usual Windows failure)."""
    if not path.exists():
        return

    def _retry(func, target, _exc):  # type: ignore[no-untyped-def]
        clear_readonly(Path(target))
        func(target)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:
            shutil.rmtree(path, onerror=lambda f, t, e: _retry(f, t, e))
    except OSError as exc:
        raise SetupError(
            f"Could not remove {path}",
            remedies=permission_remedies(exc, path),
            detail=str(exc),
        ) from exc


def copy_preserving(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, retrying once past a read-only destination."""
    try:
        shutil.copyfile(src, dst)
    except PermissionError as exc:
        # A read-only destination is the common case (a .env left over from a
        # previous checkout, or copied off a mounted image). Clear the bit and
        # try once more before giving up.
        failure: OSError = exc
        if clear_readonly(dst):
            try:
                shutil.copyfile(src, dst)
                return
            except OSError as retry_exc:
                failure = retry_exc
        raise SetupError(
            f"Could not write {dst}",
            remedies=permission_remedies(failure, dst),
            detail=str(failure),
        ) from failure
    except OSError as exc:
        raise SetupError(
            f"Could not copy {src.name} to {dst}",
            remedies=permission_remedies(exc, dst),
            detail=str(exc),
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Running commands
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CommandResult:
    argv: list[str]
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = TIMEOUT_QUICK,
    stream: bool = False,
) -> CommandResult:
    """Run *argv* and always come back with a result — never an unhandled raise.

    ``stream=True`` lets the child write straight to our terminal (used for the
    long installs, where silence for four minutes looks like a hang). Otherwise
    output is captured so a successful step stays quiet and a failed one can
    show its tail.
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        if stream:
            proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=merged, timeout=timeout)
            return CommandResult(argv, proc.returncode, "")
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=merged,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        return CommandResult(argv, proc.returncode, text)
    except FileNotFoundError:
        return CommandResult(argv, 127, f"{argv[0]}: not found on PATH")
    except PermissionError as exc:
        return CommandResult(argv, 126, f"{argv[0]}: permission denied ({exc})")
    except subprocess.TimeoutExpired:
        return CommandResult(argv, 124, f"{shlex_join(argv)}: timed out after {timeout}s")
    except OSError as exc:
        return CommandResult(argv, 125, f"{shlex_join(argv)}: {exc}")


def tool_version(executable: str, *args: str) -> str | None:
    """First line of ``<tool> --version``, or None if the tool isn't usable."""
    result = run([executable, *(args or ("--version",))], timeout=20)
    if not result.ok:
        return None
    first = result.output.strip().splitlines()
    return first[0].strip() if first else ""


# ─────────────────────────────────────────────────────────────────────────────
# Finding (or installing) uv
# ─────────────────────────────────────────────────────────────────────────────


def uv_candidate_paths() -> list[Path]:
    """Where the official installer puts uv, for when PATH hasn't caught up.

    Installing uv in *this* process does not update *our* PATH — the installer
    edits shell profiles that only affect future shells. So after installing we
    look in the known install locations directly rather than telling the user to
    open a new terminal and start over.
    """
    home = Path.home()
    if IS_WINDOWS:
        local_app = os.environ.get("LOCALAPPDATA")
        candidates = [
            home / ".local" / "bin" / "uv.exe",
            home / ".cargo" / "bin" / "uv.exe",
        ]
        if local_app:
            candidates.insert(0, Path(local_app) / "uv" / "bin" / "uv.exe")
            candidates.append(Path(local_app) / "Programs" / "uv" / "uv.exe")
        return candidates
    return [
        home / ".local" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
    ]


def find_uv() -> Path | None:
    """Locate a *working* uv: PATH first, then the standard install locations."""
    on_path = shutil.which("uv")
    if on_path and tool_version(on_path) is not None:
        return Path(on_path)
    for candidate in uv_candidate_paths():
        if candidate.is_file() and tool_version(str(candidate)) is not None:
            return candidate
    return None


def install_uv(console: Console) -> Path:
    """Install uv with the vendor's own installer, then re-locate the binary."""
    if IS_WINDOWS:
        # powershell.exe ships with Windows; pwsh is the PowerShell 7+ name and is
        # all that exists on Server Core / trimmed images. Take whichever is there.
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            raise SetupError(
                "uv is not installed, and no PowerShell was found to run its installer",
                remedies=[
                    "Install uv with winget:  winget install --id=astral-sh.uv -e",
                    "Or download it from https://docs.astral.sh/uv/getting-started/installation/",
                    "Then re-run this script.",
                ],
            )
        argv = [
            shell,
            "-NoProfile",
            # The installer is a remote script; the machine's ExecutionPolicy is
            # commonly RemoteSigned/Restricted, which would block it. Bypass is
            # scoped to this one process, not persisted to the machine.
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"irm {UV_INSTALL_URL_WINDOWS} | iex",
        ]
    elif shutil.which("curl"):
        argv = ["sh", "-c", f"curl -LsSf {UV_INSTALL_URL_UNIX} | sh"]
    elif shutil.which("wget"):
        argv = ["sh", "-c", f"wget -qO- {UV_INSTALL_URL_UNIX} | sh"]
    else:
        raise SetupError(
            "uv is not installed, and neither curl nor wget is available to fetch it",
            remedies=[
                "Install uv yourself, then re-run this script:",
                "    https://docs.astral.sh/uv/getting-started/installation/",
                "    (or: pipx install uv · brew install uv · pip install --user uv)",
            ],
        )

    console.detail("uv not found — installing it with the official installer")
    console.command(argv)
    result = run(argv, timeout=TIMEOUT_INSTALL, stream=True)
    if not result.ok:
        raise SetupError(
            f"The uv installer failed (exit {result.code})",
            remedies=[
                "Install uv manually, then re-run this script:",
                "    https://docs.astral.sh/uv/getting-started/installation/",
                "Behind a corporate proxy? Set HTTPS_PROXY (and REQUESTS_CA_BUNDLE) first.",
                "Or skip the auto-install entirely:  python bootstrap.py --no-uv-install",
            ],
            detail=result.output,
        )

    found = find_uv()
    if found is None:
        raise SetupError(
            "uv installed, but the binary is still not findable",
            remedies=[
                "Open a NEW terminal (the installer edits your shell profile) and re-run.",
                "Checked: " + ", ".join(str(p) for p in uv_candidate_paths()),
            ],
        )
    return found


def uv_supports_all_packages(uv: Path) -> bool:
    """Probe rather than version-compare — the flag is what we actually need."""
    result = run([str(uv), "sync", "--help"], timeout=30)
    return result.ok and "--all-packages" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# Repo facts
# ─────────────────────────────────────────────────────────────────────────────


def required_python() -> str:
    """The Python version the workspace pins, read from uv.lock (the lock wins).

    Falls back to a member's ``requires-python`` and finally to a constant, so a
    partially-checked-out tree still gets a sensible answer instead of a crash.
    """
    sources = [ROOT / "uv.lock", ROOT / "pyproject.toml"]
    sources.extend(sorted(PROJECTS.glob("*/pyproject.toml"))[:1])
    for source in sources:
        if not source.is_file():
            continue
        try:
            head = source.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        match = re.search(r'requires-python\s*=\s*"[^0-9"]*(\d+)\.(\d+)', head)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return FALLBACK_WORKSPACE_PYTHON


def python_projects() -> list[Path]:
    """Every uv-workspace project, i.e. the ones with a pyproject.toml."""
    if not PROJECTS.is_dir():
        return []
    return sorted(p.parent for p in PROJECTS.glob("*/pyproject.toml"))


def projects_needing_docker() -> list[str]:
    return [p.name for p in python_projects() if (p / "docker-compose.yml").is_file()]


def venv_python() -> Path:
    """Path to the interpreter inside the workspace venv, per-platform layout."""
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def activate_hint() -> str:
    if IS_WINDOWS:
        return r".venv\Scripts\activate  (PowerShell: .\.venv\Scripts\Activate.ps1)"
    return "source .venv/bin/activate"


# ─────────────────────────────────────────────────────────────────────────────
# The steps
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ORDER = {"fail": 0, "warn": 1, "ok": 2, "skip": 3}


@dataclass
class StepResult:
    name: str
    status: str
    summary: str


@dataclass
class Bootstrap:
    console: Console
    check_only: bool = False
    recreate: bool = False
    install_hooks: bool = False
    allow_uv_install: bool = True
    results: list[StepResult] = field(default_factory=list)
    uv: Path | None = None

    def record(self, name: str, status: str, summary: str) -> None:
        self.results.append(StepResult(name, status, summary))

    def mark_failed(self, reason: str) -> None:
        """A step raised before it could record itself — make the summary say so."""
        self.results.append(StepResult("failed", "fail", reason))

    # ── 1. preflight ────────────────────────────────────────────────────────
    def preflight(self) -> None:
        self.console.step("preflight")

        if sys.version_info < MIN_BOOTSTRAP_PYTHON:
            need = ".".join(str(n) for n in MIN_BOOTSTRAP_PYTHON)
            raise SetupError(
                f"This script needs Python {need}+ to run (found {platform.python_version()})",
                remedies=[f"Run it with a newer interpreter, e.g.  python3 {Path(__file__).name}"],
            )

        missing = [
            name for name in ("pyproject.toml", "uv.lock", "projects") if not (ROOT / name).exists()
        ]
        if missing:
            raise SetupError(
                f"{ROOT} does not look like the backend-gauntlet repo root",
                remedies=[
                    f"Missing: {', '.join(missing)}",
                    "Run this script from inside the clone, keeping it at the repo root.",
                ],
            )

        if not self.check_only:
            probe_writable(ROOT)

        self.console.ok(
            f"Python {platform.python_version()} on "
            f"{platform.system()} {platform.machine()} {Style.DIM}({ROOT}){Style.RESET}"
        )

        for warning in self.environment_warnings():
            self.console.warn(warning)

        count = len(python_projects())
        self.console.detail(f"{count} Python projects, workspace Python {required_python()}")
        self.record("preflight", "ok", f"{count} Python projects discovered")

    def environment_warnings(self) -> list[str]:
        """Non-fatal conditions that reliably cause trouble later if ignored."""
        warnings = []

        if not IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() == 0:
            warnings.append(
                "Running as root: the .venv it creates will be root-owned and "
                "unusable from your normal user. Prefer running as yourself."
            )

        if VENV.exists():
            owner = owner_description(VENV)
            if owner and not IS_WINDOWS and os.geteuid() != 0 and owner.startswith("root:"):
                warnings.append(
                    f"The existing .venv is owned by {owner} — a previous sudo run. "
                    "Remove it (sudo rm -rf .venv) or re-run with --recreate."
                )

        lowered = str(ROOT).lower()
        if any(marker in lowered for marker in SYNC_FOLDER_MARKERS):
            warnings.append(
                "This clone is inside a cloud-synced folder (OneDrive/Dropbox/...). "
                "The sync client locks files mid-write and corrupts virtualenvs — "
                "move the clone to a local path such as C:\\src."
            )

        if IS_WINDOWS and str(ROOT).startswith("\\\\"):
            warnings.append(
                "This clone is on a UNC/network path. Virtualenvs on network shares "
                "break in confusing ways (linking, locking, slow imports) — clone to a "
                "local drive such as C:\\src instead."
            )

        if IS_WINDOWS and len(str(ROOT)) > 150:
            warnings.append(
                f"The repo path is {len(str(ROOT))} characters. Windows' 260-char limit "
                "is easy to hit from here — consider moving the clone somewhere shallow."
            )

        return warnings

    # ── 2. uv ───────────────────────────────────────────────────────────────
    def ensure_uv(self) -> None:
        self.console.step("uv")
        found = find_uv()

        if found is None:
            if self.check_only:
                self.console.fail("uv is not installed")
                self.record("uv", "fail", "not installed")
                return
            if not self.allow_uv_install:
                raise SetupError(
                    "uv is not installed and --no-uv-install was passed",
                    remedies=["https://docs.astral.sh/uv/getting-started/installation/"],
                )
            found = install_uv(self.console)

        version = tool_version(str(found)) or "unknown version"
        self.uv = found
        self.console.ok(f"{version} {Style.DIM}({found}){Style.RESET}")

        if shutil.which("uv") is None:
            self.console.warn(
                f"uv is not on your PATH yet — this run uses {found} directly. "
                f"Add {found.parent} to PATH so `uv` works in your shell."
            )

        if not uv_supports_all_packages(found):
            raise SetupError(
                f"This uv ({version}) is too old: `uv sync` has no --all-packages",
                remedies=["Update it:  uv self update", "Or reinstall from https://astral.sh/uv"],
            )
        self.record("uv", "ok", version)

    # ── 3. interpreter ──────────────────────────────────────────────────────
    def ensure_python(self) -> None:
        wanted = required_python()
        self.console.step(f"python {wanted}")

        if self.uv is None:
            self.console.fail("skipped — uv unavailable")
            self.record("python", "fail", "skipped, no uv")
            return

        if self.check_only:
            listing = run([str(self.uv), "python", "list", "--only-installed"], timeout=60)
            have = wanted in listing.output
            self.console.say(
                f"   {self.console.g('ok' if have else 'warn')} "
                f"Python {wanted} {'is installed' if have else 'would be installed'}"
            )
            self.record(
                "python", "ok" if have else "warn", f"{wanted} {'present' if have else 'missing'}"
            )
            return

        # Idempotent: a no-op when a matching interpreter is already managed.
        # Explicit because uv's system interpreter may be older than the lock
        # requires, and the resulting error is far less obvious than this step.
        argv = [str(self.uv), "python", "install", wanted]
        self.console.command(argv)
        result = run(argv, cwd=ROOT, timeout=TIMEOUT_INSTALL)
        if not result.ok:
            raise SetupError(
                f"Could not install Python {wanted}",
                remedies=[
                    "Behind a proxy? Set HTTPS_PROXY, or install Python "
                    f"{wanted} yourself and re-run.",
                    "See:  uv python list",
                ],
                detail=result.output,
            )
        self.console.ok(f"Python {wanted} available to uv")
        self.record("python", "ok", wanted)

    # ── 4. the virtualenv ───────────────────────────────────────────────────
    def sync_workspace(self) -> None:
        self.console.step("virtualenv")

        if self.uv is None:
            self.console.fail("skipped — uv unavailable")
            self.record("venv", "fail", "skipped, no uv")
            return

        if self.check_only:
            if venv_python().is_file():
                version = tool_version(str(venv_python())) or "unknown"
                self.console.ok(f"{VENV.name} exists ({version})")
                self.record("venv", "ok", version)
            else:
                self.console.warn(f"{VENV.name} does not exist yet — would be created")
                self.record("venv", "warn", "missing")
            return

        if self.recreate and VENV.exists():
            self.console.detail(f"removing {VENV}")
            rmtree_robust(VENV)

        argv = [str(self.uv), "sync", "--all-packages", "--frozen"]
        self.console.command(argv)
        self.console.detail("first run downloads every dependency — this can take a few minutes")
        result = run(argv, cwd=ROOT, timeout=TIMEOUT_INSTALL)

        # Windows hardlink/AV failures are the single most common way this step
        # dies. uv can copy instead of linking; that is slower but always works,
        # so retry once rather than making the user discover UV_LINK_MODE.
        if not result.ok and self.is_link_failure(result.output):
            self.console.warn("hardlinking failed — retrying with UV_LINK_MODE=copy")
            result = run(argv, cwd=ROOT, env={"UV_LINK_MODE": "copy"}, timeout=TIMEOUT_INSTALL)
            if result.ok:
                self.console.detail(
                    "Set UV_LINK_MODE=copy in your environment to skip that retry next time."
                )

        if not result.ok:
            raise SetupError(
                f"`uv sync` failed (exit {result.code})",
                remedies=self.sync_remedies(result.output),
                detail=result.output,
            )

        interpreter = venv_python()
        if not interpreter.is_file():
            raise SetupError(
                f"uv sync reported success but {interpreter} is missing",
                remedies=["Try:  python bootstrap.py --recreate"],
            )

        version = tool_version(str(interpreter)) or "unknown"
        self.console.ok(f"{version} in {VENV}")
        self.record("venv", "ok", version)

    @staticmethod
    def is_link_failure(output: str) -> bool:
        lowered = output.lower()
        signatures = (
            "failed to hardlink",
            "hardlink",
            "os error 5",
            "access is denied",
            "sharing violation",
            "os error 1314",
        )
        return any(sig in lowered for sig in signatures)

    @staticmethod
    def sync_remedies(output: str) -> list[str]:
        """Turn uv's own error text into the specific next thing to try."""
        lowered = output.lower()

        if "needs to be updated" in lowered or "not up-to-date" in lowered:
            return [
                "uv.lock does not match the pyproject files — that is a repo problem, "
                "not a machine problem.",
                "Regenerate and commit it:  uv lock",
                "Then re-run this script.",
            ]
        if "no interpreter found" in lowered or "not compatible with the locked" in lowered:
            return [
                f"No Python {required_python()} interpreter is visible to uv.",
                f"    uv python install {required_python()}",
                "Then re-run this script.",
            ]
        if "no space left" in lowered or "os error 28" in lowered:
            return [
                "The disk is full. Free some space and re-run.",
                "Large removable caches: ./target (Rust builds), ./.venv, uv's cache "
                "(`uv cache clean`).",
            ]
        if "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
            return [
                "TLS/certificate failure — usually a corporate proxy.",
                "Point uv at your CA bundle:  set SSL_CERT_FILE=<path to bundle>",
                "Or:  uv sync --native-tls",
            ]
        if "permission denied" in lowered or "os error 13" in lowered:
            return [
                "Permission denied while writing the virtualenv.",
                "Remove it and start clean:  python bootstrap.py --recreate",
                "If it was created with sudo, it is root-owned:  sudo rm -rf .venv",
            ]
        return [
            "Re-run with a clean virtualenv:  python bootstrap.py --recreate",
            "If it persists, clear uv's cache:  uv cache clean",
        ]

    # ── 5. per-project .env files ───────────────────────────────────────────
    def seed_env_files(self) -> None:
        self.console.step("project .env files")

        projects = python_projects()
        created, kept, absent = [], [], []
        for project in projects:
            example = project / ".env.example"
            target = project / ".env"
            if not example.is_file():
                absent.append(project.name)
                continue
            if target.exists():
                kept.append(project.name)
                continue
            if self.check_only:
                created.append(project.name)
                continue
            copy_preserving(example, target)
            created.append(project.name)

        verb = "would create" if self.check_only else "created"
        if created:
            self.console.ok(f"{verb} .env for {len(created)}: {', '.join(created)}")
        if kept:
            self.console.detail(f"kept existing .env for {len(kept)}: {', '.join(kept)}")
        if absent:
            self.console.detail(f"no .env.example: {', '.join(absent)}")

        self.record("env", "ok", f"{len(created)} {verb}, {len(kept)} kept")

    # ── 6. git hooks (opt-in) ───────────────────────────────────────────────
    def configure_hooks(self) -> None:
        if not self.install_hooks:
            return
        self.console.step("git hooks")

        hooks_dir = ROOT / ".githooks"
        if not hooks_dir.is_dir():
            self.console.warn(".githooks is missing — skipping")
            self.record("hooks", "warn", "no .githooks directory")
            return

        if self.check_only:
            current = run(["git", "config", "--get", "core.hooksPath"], cwd=ROOT, timeout=20)
            self.console.detail(f"core.hooksPath = {current.output.strip() or '(unset)'}")
            self.record("hooks", "ok", "check only")
            return

        result = run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, timeout=20)
        if not result.ok:
            raise SetupError(
                "Could not set core.hooksPath",
                remedies=["Is this a git clone? `git status` should work here."],
                detail=result.output,
            )

        # Git needs the hook files executable on POSIX. On Windows the bit is
        # meaningless (Git for Windows runs them through its bundled sh), so we
        # simply do not try — attempting it there is what usually errors.
        if not IS_WINDOWS:
            for hook in ("pre-commit", "pre-push"):
                path = hooks_dir / hook
                if not path.is_file():
                    continue
                try:
                    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
                except OSError as exc:
                    self.console.warn(f"could not mark {hook} executable: {exc}")

        self.console.ok("core.hooksPath = .githooks")

        # Be honest about what these hooks actually gate: they run cargo fmt.
        # On a Python-only machine they would fail every single commit.
        if shutil.which("cargo") is None:
            self.console.warn(
                "These hooks run `cargo fmt` (the Rust side) and cargo is not installed — "
                "every commit will fail. Either install Rust, or bypass per commit with "
                "SKIP_GIT_HOOKS=1, or unset them:  git config --unset core.hooksPath"
            )
            self.record("hooks", "warn", "installed, but cargo is missing")
        else:
            self.record("hooks", "ok", "core.hooksPath=.githooks")

    # ── 7. doctor ───────────────────────────────────────────────────────────
    def doctor(self) -> None:
        """Report the optional tools. None of these block Python development."""
        self.console.step("optional tools")

        checks = [
            ("git", "version control, and `make hooks`"),
            ("docker", f"deps for {', '.join(projects_needing_docker()) or 'some projects'}"),
            ("make", "the per-project `make <task>` shortcuts"),
            ("bun", "the web/ frontends (project 20)"),
            ("grpcurl", "`make smoke` against the gRPC rate limiter (02)"),
            ("glow", "`make md` — markdown in the terminal"),
            ("cargo", "the Rust half of the repo (not needed for Python work)"),
        ]

        missing = []
        for tool, purpose in checks:
            path = shutil.which(tool)
            if path is None:
                missing.append(tool)
                self.console.say(
                    f"   {Style.DIM}{self.console.g('skip')} {tool:<9} not installed "
                    f"{self.console.g('arrow')} {purpose}{Style.RESET}"
                )
                continue
            version = tool_version(tool) or ""
            self.console.say(
                f"   {Style.GREEN}{self.console.g('ok')}{Style.RESET} {tool:<9} "
                f"{Style.DIM}{version[:56]}{Style.RESET}"
            )

        if shutil.which("docker") is not None:
            # `docker info` is the only way to know the daemon is actually up;
            # `docker --version` answers from the CLI alone and says nothing.
            info = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=25)
            if info.ok:
                self.console.detail(f"docker daemon reachable (server {info.output.strip()})")
            else:
                self.console.warn(
                    "docker is installed but the daemon is not reachable — "
                    "start Docker Desktop (or `sudo systemctl start docker`) before "
                    f"`make deps` in {', '.join(projects_needing_docker()) or 'a project'}."
                )

        self.record("tools", "ok", f"{len(checks) - len(missing)}/{len(checks)} present")

    # ── optional: the CI gate ───────────────────────────────────────────────
    def verify(self) -> bool:
        """Run each project's `make verify` gate — fmt-check, lint, types, tests."""
        self.console.step("verify (the same gate CI runs)")
        if self.uv is None:
            self.console.fail("skipped — uv unavailable")
            return False

        gate = [
            ("format", ["run", "ruff", "format", "--check", "."]),
            ("lint", ["run", "ruff", "check", "."]),
            ("types", ["run", "pyright"]),
            ("tests", ["run", "pytest", "-q"]),
        ]

        failures = []
        for project in python_projects():
            for label, args in gate:
                result = run([str(self.uv), *args], cwd=project, timeout=TIMEOUT_INSTALL)
                if not result.ok:
                    failures.append((project.name, label))
                    self.console.fail(f"{project.name}: {label}")
                    for line in result.output.strip().splitlines()[-15:]:
                        print(f"{Style.DIM}      {line}{Style.RESET}", file=sys.stderr)
                    break
            else:
                self.console.ok(project.name)

        if failures:
            self.record("verify", "fail", f"{len(failures)} project(s) failing")
            return False
        self.record("verify", "ok", f"{len(python_projects())} projects green")
        return True

    # ── summary ─────────────────────────────────────────────────────────────
    def summary(self, elapsed: float) -> None:
        console = self.console
        console.emit()
        console.emit(f"{Style.DIM}{'─' * 72}{Style.RESET}")
        worst = min((STATUS_ORDER[r.status] for r in self.results), default=2)
        # A step can succeed and still have told the user something important
        # (root-owned venv, dead docker daemon). Never headline that as clean.
        if worst >= 2 and console.warnings:
            worst = 1
        headline = {
            0: f"{Style.RED}setup incomplete{Style.RESET}",
            1: f"{Style.YELLOW}setup finished with warnings{Style.RESET}",
            2: f"{Style.GREEN}setup complete{Style.RESET}",
            3: f"{Style.GREEN}setup complete{Style.RESET}",
        }[worst]
        mode = " (check only — nothing was written)" if self.check_only else ""
        console.emit(
            f"{Style.BOLD}{headline}{Style.RESET}{mode}  {Style.DIM}{elapsed:.1f}s{Style.RESET}"
        )
        console.emit()

        for result in self.results:
            glyph = {
                "ok": console.g("ok"),
                "warn": console.g("warn"),
                "fail": console.g("fail"),
            }.get(result.status, console.g("skip"))
            console.emit(f"  {glyph} {result.name:<10} {Style.DIM}{result.summary}{Style.RESET}")

        if console.warnings:
            console.emit()
            console.emit(f"{Style.YELLOW}{len(console.warnings)} warning(s) above.{Style.RESET}")

        if self.check_only:
            console.emit()
            console.emit(f"{Style.DIM}Run without --check to apply.{Style.RESET}")
            return

        if worst == 0:
            console.emit()
            console.emit(
                f"{Style.DIM}Fix the error above, then re-run:  "
                f"python {Path(__file__).name}{Style.RESET}"
            )
            return

        console.emit()
        console.emit(f"{Style.BOLD}Next{Style.RESET}")
        console.emit(
            f"  {console.g('arrow')} python tools/status.py       # where every project stands"
        )
        console.emit(
            f"  {console.g('arrow')} cd projects/23-dynamodb-core && python makefile.py run"
        )
        console.emit(f"  {console.g('arrow')} activate the venv:  {activate_hint()}")
        console.emit(
            f"{Style.DIM}  (or skip activation entirely — `uv run <cmd>` from a project dir"
            f" uses it automatically){Style.RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Set up the backend-gauntlet Python development environment.",
        epilog=(
            "Examples:\n"
            "  python bootstrap.py                 full setup (safe to re-run)\n"
            "  python bootstrap.py --check         diagnose only, change nothing\n"
            "  python bootstrap.py --verify        set up, then run the CI gate\n"
            "  python bootstrap.py --recreate      rebuild a broken .venv from scratch\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is missing without installing or writing anything",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="after setup, run every project's fmt/lint/types/tests gate",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete .venv before syncing (fixes a broken or foreign virtualenv)",
    )
    parser.add_argument(
        "--hooks",
        action="store_true",
        help="also point git's core.hooksPath at .githooks (these run cargo fmt)",
    )
    parser.add_argument(
        "--no-uv-install",
        dest="allow_uv_install",
        action="store_false",
        help="never download uv; report it as missing instead",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="only warnings, errors and the summary"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _prepare_windows_console()
    args = parse_args(argv)

    if args.check and args.recreate:
        print("bootstrap.py: --check and --recreate are mutually exclusive", file=sys.stderr)
        return 2

    console = Console(quiet=args.quiet)
    console.say(
        f"{Style.BOLD}backend-gauntlet{Style.RESET} {Style.DIM}· dev environment setup{Style.RESET}"
    )

    setup = Bootstrap(
        console=console,
        check_only=args.check,
        recreate=args.recreate,
        install_hooks=args.hooks,
        allow_uv_install=args.allow_uv_install,
    )

    started = time.perf_counter()
    try:
        setup.preflight()
        setup.ensure_uv()
        setup.ensure_python()
        setup.sync_workspace()
        setup.seed_env_files()
        setup.configure_hooks()
        setup.doctor()
        verified = setup.verify() if args.verify and not args.check else True
    except SetupError as exc:
        exc.render(console)
        setup.mark_failed(exc.message)
        setup.summary(time.perf_counter() - started)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    setup.summary(time.perf_counter() - started)
    if any(r.status == "fail" for r in setup.results) or not verified:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
