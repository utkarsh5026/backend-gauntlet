#!/usr/bin/env python3
"""Refresh the README progress dashboard from `make status`.

Reads the same dashboard as `python3 tools/status.py` (NO_COLOR), wraps it in a
fenced code block, and splices it between the markers in README.md:

    <!-- status-dashboard:start -->
    ...
    <!-- status-dashboard:end -->

Usage:
    python3 tools/update_readme_status.py
    make status-readme
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
START = "<!-- status-dashboard:start -->"
END = "<!-- status-dashboard:end -->"


def status_text() -> str:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    out = subprocess.check_output(
        [sys.executable, str(ROOT / "tools" / "status.py")],
        cwd=ROOT,
        env=env,
        text=True,
    )
    # Drop leading/trailing blank lines so the fence hugs the dashboard.
    return out.strip("\n")


def render_block(dashboard: str) -> str:
    return f"{START}\n```text\n{dashboard}\n```\n{END}"


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
    block = render_block(status_text())
    updated = splice(README.read_text(), block)
    if updated == README.read_text():
        print("README progress dashboard already up to date")
        return 0
    README.write_text(updated)
    print(f"updated {README.relative_to(ROOT)} progress dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
