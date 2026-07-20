#!/usr/bin/env python3
"""Resolve which Cargo packages / frontends CI should build from changed files.

Reads:
  CHANGED_FILES_JSON — JSON array of repo-relative paths (from dorny/paths-filter)
  CI_FORCE_ALL       — "true" to force full workspace + all frontends

Writes GitHub Actions outputs:
  rust_all      — run the full workspace
  rust_any      — any Rust work to do
  packages      — space-separated `cargo -p` list (empty when rust_all)
  frontend_any  — whether to run bun frontends
  frontend_dirs — newline-separated frontend dirs to bun-build
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# (cargo package name, project dir relative to repo root)
PROJECTS: list[tuple[str, str]] = [
    ("url-shortener", "projects/01-url-shortener"),
    ("rate-limiter", "projects/02-rate-limiter"),
    ("realtime-pubsub", "projects/03-realtime-pubsub"),
    ("job-queue", "projects/04-job-queue"),
    ("metrics-pipeline", "projects/05-metrics-pipeline"),
    ("object-store", "projects/06-object-store"),
    ("distributed-cache", "projects/07-distributed-cache"),
    ("message-broker", "projects/08-message-broker"),
    ("raft-kv", "projects/09-raft-kv"),
    ("api-gateway", "projects/10-api-gateway"),
    ("vod-streaming", "projects/11-vod-streaming"),
    ("transcode-pipeline", "projects/12-transcode-pipeline"),
    ("live-ingest", "projects/13-live-ingest"),
    ("media-transport", "projects/14-media-transport"),
    ("webrtc-sfu", "projects/15-webrtc-sfu"),
    ("live-platform", "projects/16-live-platform"),
    ("global-conferencing", "projects/17-global-conferencing"),
    ("ledger-payments-core", "projects/18-ledger-payments-core"),
    ("bittorrent", "projects/19-bittorrent"),
    ("full-text-search", "projects/20-full-text-search"),
    ("workflow-engine", "projects/21-workflow-engine"),
    ("lsm-redis", "projects/22-lsm-redis"),
]

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

# Paths that affect every crate → full workspace clippy/nextest.
_WORKSPACE_PREFIXES = (
    "crates/",
    ".cargo/",
)
_WORKSPACE_FILES = frozenset(
    {
        "Cargo.toml",
        "Cargo.lock",
        "deny.toml",
        ".config/hakari.toml",
    }
)


def parse_changed_files(raw: str | None = None) -> list[str]:
    raw = raw if raw is not None else os.environ.get("CHANGED_FILES_JSON", "")
    raw = raw.strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit(f"CHANGED_FILES_JSON must be a JSON array, got {type(data).__name__}")
    return [str(p).replace("\\", "/") for p in data]


def touches_workspace_shared(files: list[str]) -> bool:
    for path in files:
        if path in _WORKSPACE_FILES:
            return True
        if any(path.startswith(prefix) for prefix in _WORKSPACE_PREFIXES):
            return True
    return False


def _frontend_root_for(project_dir: str) -> str | None:
    for fe in FRONTENDS:
        if fe.startswith(project_dir + "/"):
            return fe
    return None


def is_rust_relevant(path: str, project_dir: str) -> bool:
    """True when `path` under `project_dir` should pull that crate into CI."""
    if path != project_dir and not path.startswith(project_dir + "/"):
        return False

    rel = path[len(project_dir) :].lstrip("/")
    if not rel:
        # touched the directory entry itself — treat as relevant
        return True

    fe = _frontend_root_for(project_dir)
    if fe is not None:
        fe_rel = fe[len(project_dir) :].lstrip("/")
        if rel == fe_rel or rel.startswith(fe_rel + "/"):
            return False

    # Docs / make / compose-only edits shouldn't compile the crate.
    if rel.startswith("docs/") or rel.endswith(".md"):
        return False
    if rel in ("Makefile", "makefile.py", "docker-compose.yml", ".env.example"):
        return False

    return (
        rel.startswith("src/")
        or rel.startswith("tests/")
        or rel.startswith("benches/")
        or rel.startswith("bench/")
        or rel.startswith("migrations/")
        or rel.startswith(".sqlx/")
        or rel.startswith("proto/")
        or rel in ("Cargo.toml", "build.rs")
        or rel.endswith(".rs")
    )


def packages_for(files: list[str]) -> list[str]:
    out: list[str] = []
    for pkg, project_dir in PROJECTS:
        if any(is_rust_relevant(f, project_dir) for f in files):
            out.append(pkg)
    return out


def frontends_for(files: list[str]) -> list[str]:
    out: list[str] = []
    for fe in FRONTENDS:
        if not Path(fe, "package.json").is_file():
            continue
        if any(f == fe or f.startswith(fe + "/") for f in files):
            out.append(fe)
    return out


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


def resolve(files: list[str], force_all: bool) -> tuple[bool, list[str], list[str]]:
    if force_all:
        all_fe = [d for d in FRONTENDS if Path(d, "package.json").is_file()]
        return True, [], all_fe

    if touches_workspace_shared(files):
        # Shared crates / lockfile → full rust; frontends only if their files changed.
        return True, [], frontends_for(files)

    return False, packages_for(files), frontends_for(files)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return self_test()

    force_all = os.environ.get("CI_FORCE_ALL", "").lower() in ("1", "true", "yes")
    files = parse_changed_files()
    rust_all, packages, frontend_dirs = resolve(files, force_all)
    rust_any = rust_all or bool(packages)

    write_output("rust_all", "true" if rust_all else "false")
    write_output("rust_any", "true" if rust_any else "false")
    write_output("packages", " ".join(packages))
    write_output("frontend_any", "true" if frontend_dirs else "false")
    write_output("frontend_dirs", "\n".join(frontend_dirs))

    if rust_all:
        print("::notice::Rust scope: full workspace", file=sys.stderr)
    elif packages:
        print(f"::notice::Rust scope: {' '.join(packages)}", file=sys.stderr)
    else:
        print("::notice::Rust scope: none", file=sys.stderr)

    if frontend_dirs:
        print(f"::notice::Frontends: {', '.join(frontend_dirs)}", file=sys.stderr)
    else:
        print("::notice::Frontends: none", file=sys.stderr)

    print(f"::notice::Changed files ({len(files)}): {', '.join(files[:20])}{'…' if len(files) > 20 else ''}", file=sys.stderr)
    return 0


def self_test() -> int:
    # Scoped: only url-shortener rust + its dashboard.
    rust_all, pkgs, fes = resolve(
        [
            "projects/01-url-shortener/src/cache.rs",
            "projects/01-url-shortener/dashboard/src/App.tsx",
            "projects/04-job-queue/src/scheduler.rs",
        ],
        force_all=False,
    )
    assert rust_all is False
    assert pkgs == ["url-shortener", "job-queue"], pkgs
    assert fes == ["projects/01-url-shortener/dashboard"], fes

    # Frontend-only → no rust package for that project.
    rust_all, pkgs, fes = resolve(
        ["projects/06-object-store/web/src/main.tsx"],
        force_all=False,
    )
    assert rust_all is False
    assert pkgs == []
    assert fes == ["projects/06-object-store/web"]

    # Docs-only → skip rust.
    rust_all, pkgs, _ = resolve(
        ["projects/01-url-shortener/SPEC.md", "projects/01-url-shortener/docs/foo.md"],
        force_all=False,
    )
    assert rust_all is False
    assert pkgs == []

    # sqlx cache counts as rust.
    _, pkgs, _ = resolve(
        ["projects/01-url-shortener/.sqlx/query-abc.json"],
        force_all=False,
    )
    assert pkgs == ["url-shortener"]

    # Shared crate → full workspace.
    rust_all, pkgs, _ = resolve(["crates/common-config/src/lib.rs"], force_all=False)
    assert rust_all is True
    assert pkgs == []

    # CI workflow alone → no rust (scoped PRs stay fast).
    rust_all, pkgs, _ = resolve([".github/workflows/ci.yml"], force_all=False)
    assert rust_all is False
    assert pkgs == []

    print("self-test ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
