#!/usr/bin/env python3
"""backend-gauntlet — one-command dev stack for a project.

`make dev NN=01` (or `python3 tools/dev.py 01 [03 ...]`) inspects the project
directory and launches one mprocs session with a pane per moving part:

  deps     docker compose up (foreground, streams service logs)     — if compose file
  server   compose up -d --wait → sqlx migrate run → cargo watch -x run
  <web>    bun install (first run) + bun run dev                    — per frontend dir

Nothing is configured per project: panes are derived from what exists on disk
(docker-compose.yml, src/main.rs, migrations/, web|dashboard|ui|frontend/).
Passing several NNs merges their stacks into one session with `NN:`-prefixed
panes — host-port scoping (54NN, 63NN, …) keeps them collision-free.

No args prints what each project would launch. `--print` dumps the generated
mprocs config instead of launching. Stdlib only, like status.py / infra.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
FRONTEND_DIRS = ("web", "dashboard", "ui", "frontend")


def find_project(nn: str) -> Path:
    if nn.isdigit():
        nn = f"{int(nn):02d}"
    hits = sorted(PROJECTS.glob(f"{nn}-*"))
    if not hits:
        sys.exit(f"error: no projects/{nn}-* directory")
    return hits[0]


def compose_file(proj: Path) -> Path | None:
    for name in ("docker-compose.yml", "compose.yml"):
        if (proj / name).exists():
            return proj / name
    return None


def frontends(proj: Path) -> list[Path]:
    return [proj / d for d in FRONTEND_DIRS if (proj / d / "package.json").exists()]


def panes(proj: Path, prefix: str = "") -> dict[str, dict]:
    """Pane name → mprocs proc entry, derived from what the project has on disk."""
    out: dict[str, dict] = {}
    compose = compose_file(proj)

    if compose:
        out[f"{prefix}deps"] = {"shell": "docker compose up", "cwd": str(proj)}

    if (proj / "src" / "main.rs").exists():
        steps = []
        if compose:
            steps.append("docker compose up -d --wait")
        if (proj / "migrations").is_dir():
            steps.append("[ -f .env ] && sqlx migrate run")
        steps.append("exec cargo watch -q -x run")
        out[f"{prefix}server"] = {"shell": "; ".join(steps), "cwd": str(proj)}

    for fe in frontends(proj):
        out[f"{prefix}{fe.name}"] = {
            "shell": "[ -d node_modules ] || bun install; exec bun run dev",
            "cwd": str(fe),
        }
    return out


def overview() -> None:
    print("usage: make dev NN=01   (multi: NN=\"01 03\")\n")
    print(f"  {'project':<28} panes")
    for proj in sorted(PROJECTS.iterdir()):
        if not (proj / "Cargo.toml").exists():
            continue
        print(f"  {proj.name:<28} {', '.join(panes(proj)) or '—'}")


def main(argv: list[str]) -> None:
    show = "--print" in argv
    nums = [a for a in argv if a != "--print"]
    if not nums:
        overview()
        return

    multi = len(nums) > 1
    procs: dict[str, dict] = {}
    for nn in nums:
        proj = find_project(nn)
        procs.update(panes(proj, prefix=f"{proj.name[:2]}:" if multi else ""))
    if not procs:
        sys.exit("error: nothing to run (no compose file, src/main.rs, or frontend found)")

    cfg = {"procs": procs}
    if show:
        print(json.dumps(cfg, indent=2))
        return

    if not shutil.which("mprocs"):
        sys.exit("error: mprocs not found — install with `cargo install mprocs`")
    # JSON is valid YAML, so hand mprocs the config without needing a yaml lib.
    path = Path(tempfile.mkdtemp(prefix="gauntlet-dev-")) / "mprocs.yaml"
    path.write_text(json.dumps(cfg, indent=2))
    os.execvp("mprocs", ["mprocs", "--config", str(path)])


if __name__ == "__main__":
    main(sys.argv[1:])
