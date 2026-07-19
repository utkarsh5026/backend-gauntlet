#!/usr/bin/env python3
"""Refresh the README progress dashboard from `make status`.

Runs the same dashboard as `python3 tools/status.py` with colors forced on,
renders it to a terminal-style SVG (so GitHub shows the greens/yellows/reds
that a plain ```text``` fence cannot), and splices an <img> between the
markers in README.md:

    <!-- status-dashboard:start -->
    ...
    <!-- status-dashboard:end -->

Experiment: image instead of a fenced text block — see
`assets/status-dashboard.svg`.

Usage:
    python3 tools/update_readme_status.py
    make status-readme

Requires: `rich` (see tools/requirements.txt).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ASSET = ROOT / "assets" / "status-dashboard.svg"
START = "<!-- status-dashboard:start -->"
END = "<!-- status-dashboard:end -->"

# Match status.py's Catppuccin-Mocha-ish feel for the SVG chrome (window
# background / default text). Per-cell truecolor from the ANSI escapes is
# preserved regardless of this theme.
try:
    from rich.terminal_theme import TerminalTheme
except ImportError:  # pragma: no cover - import deferred until render time too
    TerminalTheme = None  # type: ignore[misc, assignment]

CATPPUCCIN_MOCHA = None
if TerminalTheme is not None:
    CATPPUCCIN_MOCHA = TerminalTheme(
        (30, 30, 46),  # base background
        (205, 214, 244),  # text
        [
            (69, 71, 90),  # black (surface0)
            (243, 139, 168),  # red
            (166, 227, 161),  # green
            (249, 226, 175),  # yellow
            (137, 180, 250),  # blue
            (203, 166, 247),  # magenta / mauve
            (137, 220, 235),  # cyan / sky
            (186, 194, 222),  # white / subtext
        ],
        [
            (88, 91, 112),  # bright black
            (243, 139, 168),
            (166, 227, 161),
            (249, 226, 175),
            (137, 180, 250),
            (203, 166, 247),
            (137, 220, 235),
            (205, 214, 244),
        ],
    )


def status_ansi() -> str:
    """Run status.py with colors forced (even when stdout isn't a TTY).

    STATUS_README=1 asks for the clean screenshot layout: full
    `verticals` / `checklist` headers, airier rows, no footer legend.
    """
    env = {
        **os.environ,
        "FORCE_COLOR": "1",
        "STATUS_README": "1",
        "COLORTERM": "truecolor",
        "TERM": "xterm-256color",
    }
    env.pop("NO_COLOR", None)
    out = subprocess.check_output(
        [sys.executable, str(ROOT / "tools" / "status.py")],
        cwd=ROOT,
        env=env,
        text=True,
    )
    # Normalize PTY-ish CRs if any tooling injects them later.
    return out.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"


def render_svg(ansi: str, dest: Path) -> None:
    """Paint the ANSI dashboard into a Rich terminal SVG screenshot."""
    import io

    try:
        from rich.ansi import AnsiDecoder
        from rich.console import Console
        from rich.terminal_theme import MONOKAI
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "rich is required for the README status screenshot — "
            "pip install -r tools/requirements.txt"
        ) from e

    # Wide enough for `verticals` / `checklist` headers; file=StringIO keeps
    # the render quiet (record=True still captures what would have been printed).
    # soft_wrap=True is load-bearing: Rich's default word-wrap breaks at the
    # padding spaces before the checklist column, shoving `N/M` onto the next
    # row even when the line is well under `width`.
    console = Console(
        record=True,
        width=120,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        soft_wrap=True,
        file=io.StringIO(),
    )
    for line in AnsiDecoder().decode(ansi):
        console.print(line, overflow="ignore", crop=False)

    dest.parent.mkdir(parents=True, exist_ok=True)
    theme = CATPPUCCIN_MOCHA or MONOKAI
    console.save_svg(str(dest), title="make status", theme=theme)

    # <img src="…svg"> cannot load CDN @font-face URLs in most browsers —
    # swap Rich's Fira Code CDN block for a boring system mono stack so the
    # screenshot still looks like a terminal on GitHub.
    svg = dest.read_text()
    svg = _prefer_system_mono(svg)
    dest.write_text(svg)


def _prefer_system_mono(svg: str) -> str:
    """Drop CDN @font-face blocks; keep a mono stack that works under <img>."""
    import re

    mono = (
        "ui-monospace, SFMono-Regular, Menlo, Consolas, "
        '"Liberation Mono", monospace'
    )
    # Rich emits Regular + Bold @font-face blocks pointing at cdnjs — browsers
    # block those loads for SVGs used as <img>, so delete the faces entirely.
    svg = re.sub(r"@font-face\s*\{.*?\}\s*", "", svg, flags=re.DOTALL)
    svg = re.sub(
        r"font-family:\s*Fira Code[^;]*;",
        f"font-family: {mono};",
        svg,
    )
    svg = re.sub(
        r'font-family:\s*ui-monospace[^;]*;',
        f"font-family: {mono};",
        svg,
    )
    return svg


def render_block() -> str:
    """Markdown/HTML snipped between the README markers."""
    # Cache-bust query so GitHub's camo CDN picks up regenerations on the
    # same path (it keys on URL; without this, an updated SVG can look stale).
    # The file content hash keeps the bust stable across no-op regenerations.
    import hashlib

    digest = hashlib.sha256(ASSET.read_bytes()).hexdigest()[:12]
    rel = ASSET.relative_to(ROOT).as_posix()
    return (
        f"{START}\n"
        f'<p align="center">\n'
        f'  <img src="{rel}?h={digest}" alt="backend-gauntlet progress dashboard '
        f'(make status)" width="100%" />\n'
        f"</p>\n"
        f"{END}"
    )


def splice(readme: str, block: str) -> str:
    if START not in readme or END not in readme:
        raise SystemExit(
            f"README.md is missing {START!r} / {END!r} markers — "
            "add a Progress section with those HTML comments."
        )
    before, rest = readme.split(START, 1)
    _, after = rest.split(END, 1)
    return before + block + after


def main() -> int:
    ansi = status_ansi()
    render_svg(ansi, ASSET)
    block = render_block()
    updated = splice(README.read_text(), block)
    readme_changed = updated != README.read_text()
    if readme_changed:
        README.write_text(updated)

    rel_asset = ASSET.relative_to(ROOT)
    if readme_changed:
        print(f"updated {README.relative_to(ROOT)} + {rel_asset}")
    else:
        # Asset may still have been rewritten identically; say so either way.
        print(f"README progress dashboard already up to date ({rel_asset} refreshed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
