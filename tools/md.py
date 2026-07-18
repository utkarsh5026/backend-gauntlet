#!/usr/bin/env python3
"""backend-gauntlet — view a project's markdown in glow.

Usage:
    make md NN=01                 # interactive picker
    make md NN=01 FILE=SPEC.md    # open directly
    python3 tools/md.py 01
    python3 tools/md.py --project-dir projects/01-url-shortener
    python3 tools/md.py --project-dir . --file SPEC.md

Uses tools/glow/styles/roomy.json for airier spacing when present.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
TOOLS = ROOT / "tools"
ROOMY_STYLE = TOOLS / "glow" / "styles" / "roomy.json"
HOME_ROOMY = Path.home() / ".config" / "glow" / "styles" / "roomy.json"

SKIP_DIR_NAMES = {
    "node_modules",
    "target",
    ".git",
    "dist",
    ".sqlx",
    "__pycache__",
    ".venv",
    "venv",
}

PRIORITY_ROOT = ("SPEC.md", "CONCEPTS.md", "RESEARCH.md")
SHORTHAND = {
    "spec": "SPEC.md",
    "concepts": "CONCEPTS.md",
    "research": "RESEARCH.md",
}


def find_project(nn: str) -> Path:
    if nn.isdigit():
        nn = f"{int(nn):02d}"
    hits = sorted(PROJECTS.glob(f"{nn}-*"))
    if not hits:
        sys.exit(f"error: no projects/{nn}-* directory")
    return hits[0]


def list_projects() -> list[Path]:
    return sorted(p for p in PROJECTS.iterdir() if p.is_dir() and p.name[:2].isdigit())


def find_glow() -> str | None:
    found = shutil.which("glow")
    if found:
        return found
    go_bin = Path.home() / "go" / "bin" / "glow"
    if go_bin.is_file() and os.access(go_bin, os.X_OK):
        return str(go_bin)
    return None


def collect_markdown(project: Path) -> list[Path]:
    files: list[Path] = []
    for path in project.rglob("*.md"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        # Also skip nested web/node_modules already covered; keep web/README.md.
        files.append(path)

    def sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(project).as_posix()
        name = p.name
        if name in PRIORITY_ROOT and p.parent == project:
            return (0, PRIORITY_ROOT.index(name), rel)
        if rel.startswith("docs/"):
            return (1, 0, rel)
        return (2, 0, rel)

    return sorted(files, key=sort_key)


def resolve_file(project: Path, file_arg: str) -> Path:
    key = file_arg.strip()
    lower = key.lower()
    if lower in SHORTHAND:
        candidate = project / SHORTHAND[lower]
        if candidate.is_file():
            return candidate

    path = Path(key).expanduser()
    if not path.is_absolute():
        path = project / path
    if path.is_file():
        return path.resolve()

    # Basename match among collected files
    matches = [p for p in collect_markdown(project) if p.name.lower() == lower]
    if len(matches) == 1:
        return matches[0]
    if matches:
        sys.exit(
            "error: ambiguous file "
            f"{file_arg!r}; matches: "
            + ", ".join(p.relative_to(project).as_posix() for p in matches)
        )
    sys.exit(f"error: markdown file not found: {file_arg}")


def pick_file(project: Path, files: list[Path]) -> Path:
    """Arrow-key select (↑/↓ + Enter), same feel as Vite's create prompts."""
    from makefile_help import ensure_deps

    ensure_deps()
    import questionary
    from questionary import Choice, Style

    style = Style(
        [
            ("qmark", "fg:magenta bold"),
            ("question", "bold"),
            ("answer", "fg:cyan bold"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan bold"),
            ("selected", "fg:cyan"),
            ("instruction", "fg:ansibrightblack"),
        ]
    )
    choices = [
        Choice(
            title=path.relative_to(project).as_posix(),
            value=path,
        )
        for path in files
    ]
    selected = questionary.select(
        f"{project.name} — pick a markdown file",
        choices=choices,
        style=style,
        instruction="(↑/↓ move, enter select, ctrl-c cancel)",
        qmark="📖",
    ).ask()
    if selected is None:
        sys.exit(130)
    return selected


def style_path() -> Path | None:
    if ROOMY_STYLE.is_file():
        return ROOMY_STYLE
    if HOME_ROOMY.is_file():
        return HOME_ROOMY
    return None


def open_in_glow(path: Path) -> None:
    glow = find_glow()
    if not glow:
        sys.exit(
            "error: glow not found on PATH (or ~/go/bin/glow).\n"
            "  install: go install github.com/charmbracelet/glow@latest"
        )

    cmd = [glow, "-w", "72", "-p"]
    style = style_path()
    if style is not None:
        cmd.extend(["-s", str(style)])
    cmd.append(str(path))

    # Replace this process so glow owns the TTY (pager / keys work).
    os.execvp(cmd[0], cmd)


def usage_overview() -> None:
    print("usage: make md NN=01 [FILE=SPEC.md]\n")
    print(f"  {'project':<28} markdown files")
    for proj in list_projects():
        files = collect_markdown(proj)
        if not files:
            continue
        preview = ", ".join(p.relative_to(proj).as_posix() for p in files[:4])
        more = f" (+{len(files) - 4})" if len(files) > 4 else ""
        print(f"  {proj.name:<28} {preview}{more}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="View a project's markdown in glow",
    )
    parser.add_argument(
        "nn",
        nargs="?",
        help="project number, e.g. 01 or 1",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="project directory (skips NN lookup)",
    )
    parser.add_argument(
        "--file",
        "-f",
        dest="file",
        help="markdown path or shorthand (spec, concepts, research)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    # Allow FILE= from the environment when invoked via make without --file.
    env_file = os.environ.get("FILE")
    args = parse_args(argv)
    file_arg = args.file or env_file

    if args.project_dir is not None:
        project = args.project_dir.resolve()
        if not project.is_dir():
            sys.exit(f"error: not a directory: {project}")
    elif args.nn:
        project = find_project(args.nn)
    else:
        usage_overview()
        sys.exit(2)

    files = collect_markdown(project)
    if not files:
        sys.exit(f"error: no markdown files under {project.name}")

    if file_arg:
        target = resolve_file(project, file_arg)
    elif len(files) == 1:
        target = files[0]
    else:
        # Non-interactive: if stdin is not a TTY, require FILE=
        if not sys.stdin.isatty():
            sys.exit(
                "error: multiple markdown files; pass FILE=… or --file … "
                f"(found {len(files)} under {project.name})"
            )
        target = pick_file(project, files)

    open_in_glow(target)


if __name__ == "__main__":
    tools = str(TOOLS)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    main(sys.argv[1:])
