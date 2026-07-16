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

Pane discovery lives in ``makefile_runner`` so per-project ``make dev`` (via
``register_dev_stack``) uses the same rules.

No args prints what each project would launch. `--print` dumps the generated
mprocs config instead of launching. Stdlib only, like status.py / infra.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from makefile_runner import discover_dev_panes, launch_mprocs

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"


def find_project(nn: str) -> Path:
    if nn.isdigit():
        nn = f"{int(nn):02d}"
    hits = sorted(PROJECTS.glob(f"{nn}-*"))
    if not hits:
        sys.exit(f"error: no projects/{nn}-* directory")
    return hits[0]


def overview() -> None:
    print("usage: make dev NN=01   (multi: NN=\"01 03\")\n")
    print(f"  {'project':<28} panes")
    for proj in sorted(PROJECTS.iterdir()):
        if not (proj / "Cargo.toml").exists():
            continue
        panes = discover_dev_panes(proj)
        print(f"  {proj.name:<28} {', '.join(panes) or '—'}")


def main(argv: list[str]) -> None:
    show = "--print" in argv
    nums = [a for a in argv if a != "--print"]
    if not nums:
        overview()
        return

    multi = len(nums) > 1
    procs: dict[str, dict[str, str]] = {}
    for nn in nums:
        proj = find_project(nn)
        procs.update(
            discover_dev_panes(proj, prefix=f"{proj.name[:2]}:" if multi else "")
        )
    if not procs:
        sys.exit("error: nothing to run (no compose file, src/main.rs, or frontend found)")

    if show:
        print(json.dumps({"procs": procs}, indent=2))
        return

    launch_mprocs(procs)


if __name__ == "__main__":
    main(sys.argv[1:])
