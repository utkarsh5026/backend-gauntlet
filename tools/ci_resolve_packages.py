#!/usr/bin/env python3
"""Resolve which frontends CI should build from changed files.

Reads:
  CHANGED_FILES_JSON — JSON array of repo-relative paths (from dorny/paths-filter)
  CI_FORCE_ALL       — "true" to force every frontend

Writes GitHub Actions outputs:
  frontend_any  — whether to run bun frontends
  frontend_dirs — newline-separated frontend dirs to bun-build

Python scope is not decided here: the `python` paths-filter in ci.yml gates that
job, and it runs every project's `make verify` gate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Frontend dirs (must contain package.json to be built).
FRONTENDS: list[str] = [
    "projects/01-url-shortener/dashboard",
    "projects/03-realtime-pubsub/web",
    "projects/04-job-queue/web",
    "projects/06-object-store/web",
    "projects/11-vod-streaming/web",
    "projects/13-live-ingest/web",
    "projects/15-webrtc-sfu/web",
    "projects/16-live-platform/web",
    "projects/17-global-conferencing/web",
    "projects/20-full-text-search/web",
]


def parse_changed_files(raw: str | None = None) -> list[str]:
    raw = raw if raw is not None else os.environ.get("CHANGED_FILES_JSON", "")
    raw = raw.strip()
    if not raw:
        return []
    data: object = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit(f"CHANGED_FILES_JSON must be a JSON array, got {type(data).__name__}")
    return [str(p).replace("\\", "/") for p in data]  # pyright: ignore[reportUnknownVariableType]


def frontends_for(files: list[str]) -> list[str]:
    out: list[str] = []
    for fe in FRONTENDS:
        if not Path(fe, "package.json").is_file():
            continue
        if any(f == fe or f.startswith(fe + "/") for f in files):
            out.append(fe)
    return out


def resolve(files: list[str], force_all: bool) -> list[str]:
    if force_all:
        return [d for d in FRONTENDS if Path(d, "package.json").is_file()]
    return frontends_for(files)


def write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            if "\n" in value:
                fh.write(f"{key}<<EOF\n{value}\nEOF\n")
            else:
                fh.write(f"{key}={value}\n")
    summary = value.replace("\n", ",") if "\n" in value else value
    print(f"{key}={summary}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return self_test()

    force_all = os.environ.get("CI_FORCE_ALL", "").lower() in ("1", "true", "yes")
    files = parse_changed_files()
    frontend_dirs = resolve(files, force_all)

    write_output("frontend_any", "true" if frontend_dirs else "false")
    write_output("frontend_dirs", "\n".join(frontend_dirs))

    if frontend_dirs:
        print(f"::notice::Frontends: {', '.join(frontend_dirs)}", file=sys.stderr)
    else:
        print("::notice::Frontends: none", file=sys.stderr)
    shown = ", ".join(files[:20]) + ("…" if len(files) > 20 else "")
    print(f"::notice::Changed files ({len(files)}): {shown}", file=sys.stderr)
    return 0


def self_test() -> int:
    # A frontend change builds that frontend; Python beside it does not add one.
    fes = resolve(
        [
            "projects/13-live-ingest/src/live_ingest/rtmp.py",
            "projects/06-object-store/web/src/App.tsx",
        ],
        force_all=False,
    )
    assert fes == ["projects/06-object-store/web"], fes

    # Python-only and docs-only changes build no frontend.
    assert resolve(["projects/06-object-store/src/object_store/index.py"], False) == []
    assert resolve(["projects/06-object-store/SPEC.md", "docs/foo.md"], False) == []

    # A path that merely shares a prefix is not inside the frontend dir.
    assert resolve(["projects/06-object-store/webhooks.py"], False) == []

    # Forcing builds every frontend that actually has a package.json.
    assert resolve([], force_all=True) == [
        d for d in FRONTENDS if Path(d, "package.json").is_file()
    ]

    print("self-test ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
