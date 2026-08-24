#!/usr/bin/env python3
"""full-text-search — local dev task runner.

A small wrapper around the day-to-day commands for this project (uv, docker, the
index probes). The `Makefile` shells out to this file so you get one source of
truth with colors, emojis and readable output. Help tables use
`tools/makefile_help.py` (Rich — auto-installed from `tools/requirements.txt`).

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    make_runner,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
    register_smoke_healthz,
)

runner = make_runner(
    crate="full-text-search",
    help_title="🔍 full-text-search",
    project_dir=PROJECT_DIR,
    default_port="9200",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Index something", "make index && make refresh && make search Q=rust"),
        ("Before you commit", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
register_smoke_healthz(runner)


def _base_url() -> str:
    port = runner.load_dotenv().get("PORT", runner.config.default_port)
    return f"http://localhost:{port}"


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


@runner.task("index", "📥", "Run", "Bulk-index a tiny sample corpus (server must be running)")
def index() -> None:
    """The three-command loop the SPEC's near-real-time contract describes:
    index, refresh, search. Split into separate targets on purpose — watching
    `make search` return nothing until `make refresh` runs is the fastest way to
    internalise why search is "near-real-time"."""
    runner.require("curl", "Install curl to use this target.")
    corpus = "\n".join(
        [
            '{"id":"d1","text":"Rust is a systems programming language focused on safety"}',
            '{"id":"d2","text":"The inverted index maps each term to the documents containing it"}',
            '{"id":"d3","text":"BM25 ranks documents by relevance, not just by matching"}',
            '{"id":"d4","text":"Segments are immutable; merging compacts them and reclaims space"}',
        ]
    )
    runner.step("📥", f"POST {_base_url()}/_bulk (4 documents)")
    runner.run(
        ["curl", "-sS", "-X", "POST", "--data-binary", corpus, f"{_base_url()}/_bulk"],
        check=False,
    )
    print()


@runner.task("refresh", "🔄", "Run", "Flush buffered docs into segments (makes them searchable)")
def refresh() -> None:
    runner.require("curl", "Install curl to use this target.")
    runner.step("🔄", f"POST {_base_url()}/_refresh")
    runner.run(["curl", "-sS", "-X", "POST", f"{_base_url()}/_refresh"], check=False)
    print()


@runner.task("search", "🔍", "Run", "Search the index — Q='your query' (default: rust)")
def search() -> None:
    runner.require("curl", "Install curl to use this target.")
    import os

    query = os.environ.get("Q", "rust")
    runner.step("🔍", f"GET {_base_url()}/search?q={query}")
    runner.run(
        ["curl", "-sSG", "--data-urlencode", f"q={query}", f"{_base_url()}/search"],
        check=False,
    )
    print()


@runner.task("stats", "📊", "Run", "Per-shard segment + document counts")
def stats() -> None:
    runner.require("curl", "Install curl to use this target.")
    runner.step("📊", f"GET {_base_url()}/_stats")
    runner.run(["curl", "-sS", f"{_base_url()}/_stats"], check=False)
    print()


@runner.task("reset", "🧨", "Run", "Delete the on-disk index (INDEX_DIR) — starts empty")
def reset() -> None:
    """The index is the filesystem, so 'start over' is `rm -rf`.

    Worth having as a target rather than a habit: a stale segment from a previous
    format is the most confusing bug in V2, because it parses as *something*.
    """
    index_dir = Path(runner.load_dotenv().get("INDEX_DIR", "./data"))
    if not index_dir.is_absolute():
        index_dir = runner.project_dir / index_dir
    if not index_dir.exists():
        runner.ok(f"{index_dir} does not exist — nothing to reset")
        return
    shutil.rmtree(index_dir)
    runner.ok(f"removed {index_dir}")


@runner.task("profile", "🔥", "Bench", "Sample the running engine with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so drive load at it while this runs —
    a flamegraph of an idle server tells you nothing. Expect the top frames to be
    the V3 scoring loop; if they are not, that itself is the finding.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    runner.step("🔥", "sampling for 10s — drive some searches at the engine meanwhile")
    runner.uv(
        "run",
        "py-spy",
        "record",
        "--duration",
        "10",
        "--output",
        str(out),
        "--",
        "python",
        "-c",
        "import full_text_search.main as m; m.main()",
    )
    runner.ok(f"wrote {out}")


@runner.task("dev", "🛠️", "Run", "sync + run the engine")
def dev() -> None:
    sync()
    run = runner.tasks["run"][0]
    run()


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
