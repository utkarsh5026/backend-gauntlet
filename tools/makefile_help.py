"""Shared Rich help tables for per-project ``makefile.py`` runners."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REQUIREMENTS = TOOLS_DIR / "requirements.txt"


def ensure_deps() -> None:
    """Install ``tools/requirements.txt`` when a required package is missing."""
    missing = False
    try:
        import rich  # noqa: F401
    except ImportError:
        missing = True
    try:
        import questionary  # noqa: F401
    except ImportError:
        missing = True
    if missing:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"],
            check=True,
        )


def print_project_help(
    *,
    title: str,
    tasks: Mapping[str, tuple[Any, ...]],
    subtitle: str = "common commands",
    skip: Sequence[str] = ("help",),
    footers: Sequence[tuple[str, str]] = (),
) -> None:
    """Render grouped ``make`` task tables with Rich.

    Each ``tasks`` entry is ``(fn, emoji, group, help_text)``.
    ``footers`` is a list of ``(bold label, dim body)`` lines printed after the tables.
    """
    ensure_deps()
    from rich.cells import set_cell_size
    from rich.console import Console
    from rich.padding import Padding
    from rich.table import Table

    console = Console()
    console.print(f"\n[bold magenta]{title}[/] [dim]— {subtitle}[/]")

    groups: dict[str, list[str]] = {}
    for name, entry in tasks.items():
        if name in skip:
            continue
        groups.setdefault(entry[2], []).append(name)

    cmd_width = max(
        len(f"make {name}")
        for name in tasks
        if name not in skip
    )

    for group, names in groups.items():
        console.print(f"\n[bold yellow]{group}[/]")
        # Borderless grid: aligned columns, no heavy boxes or full-width stretch.
        grid = Table.grid(padding=(0, 3))
        grid.add_column(no_wrap=True, min_width=cmd_width + 4)
        grid.add_column(overflow="fold")
        for name in names:
            _, emoji, _, help_text = tasks[name]
            # Normalise every emoji to a fixed 2-cell width so the command
            # text lines up regardless of variation-selector glyph widths,
            # then keep emoji + command in one cell (no stray emoji column).
            label = f"{set_cell_size(emoji, 2)}  [cyan]make {name}[/]"
            grid.add_row(label, f"[dim]{help_text}[/]")
        console.print(Padding(grid, (0, 0, 0, 2), expand=False))

    console.print()
    for label, text in footers:
        console.print(f"[bold]{label}:[/] [dim]{text}[/]")
    if footers:
        console.print()
