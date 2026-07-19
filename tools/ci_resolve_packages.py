#!/usr/bin/env python3
"""Resolve which Cargo packages / frontends CI should build from path-filter outputs.

Reads `FILTER_<name>=true|false` env vars (dorny/paths-filter style) and writes
GitHub Actions outputs:
  rust_all   — run the full workspace
  rust_any   — any Rust work to do
  packages   — space-separated `cargo -p` list (empty when rust_all)
  frontend_dirs — newline-separated frontend dirs to bun-build (empty = none)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# (filter_name, cargo package name, project dir relative to repo root)
PROJECTS: list[tuple[str, str, str]] = [
    ("url_shortener", "url-shortener", "projects/01-url-shortener"),
    ("rate_limiter", "rate-limiter", "projects/02-rate-limiter"),
    ("realtime_pubsub", "realtime-pubsub", "projects/03-realtime-pubsub"),
    ("job_queue", "job-queue", "projects/04-job-queue"),
    ("metrics_pipeline", "metrics-pipeline", "projects/05-metrics-pipeline"),
    ("object_store", "object-store", "projects/06-object-store"),
    ("distributed_cache", "distributed-cache", "projects/07-distributed-cache"),
    ("message_broker", "message-broker", "projects/08-message-broker"),
    ("raft_kv", "raft-kv", "projects/09-raft-kv"),
    ("api_gateway", "api-gateway", "projects/10-api-gateway"),
    ("vod_streaming", "vod-streaming", "projects/11-vod-streaming"),
    ("transcode_pipeline", "transcode-pipeline", "projects/12-transcode-pipeline"),
    ("live_ingest", "live-ingest", "projects/13-live-ingest"),
    ("media_transport", "media-transport", "projects/14-media-transport"),
    ("webrtc_sfu", "webrtc-sfu", "projects/15-webrtc-sfu"),
    ("live_platform", "live-platform", "projects/16-live-platform"),
    ("global_conferencing", "global-conferencing", "projects/17-global-conferencing"),
    ("ledger_payments", "ledger-payments-core", "projects/18-ledger-payments-core"),
    ("bittorrent", "bittorrent", "projects/19-bittorrent"),
    ("full_text_search", "full-text-search", "projects/20-full-text-search"),
    ("workflow_engine", "workflow-engine", "projects/21-workflow-engine"),
    ("lsm_redis", "lsm-redis", "projects/22-lsm-redis"),
]

# (filter_name, directory containing package.json)
FRONTENDS: list[tuple[str, str]] = [
    ("fe_01", "projects/01-url-shortener/dashboard"),
    ("fe_03", "projects/03-realtime-pubsub/web"),
    ("fe_04", "projects/04-job-queue/web"),
    ("fe_06", "projects/06-object-store/web"),
    ("fe_11", "projects/11-vod-streaming/web"),
    ("fe_13", "projects/13-live-ingest/web"),
    ("fe_15", "projects/15-webrtc-sfu/web"),
    ("fe_16", "projects/16-live-platform/web"),
    ("fe_17", "projects/17-global-conferencing/web"),
    ("fe_20", "projects/20-full-text-search/web"),
]


def flag(name: str) -> bool:
    return os.environ.get(f"FILTER_{name}", "false").lower() == "true"


def write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            if "\n" in value:
                fh.write(f"{key}<<EOF\n{value}\nEOF\n")
            else:
                fh.write(f"{key}={value}\n")
    # Always log a single-line summary for the job transcript.
    summary = value.replace("\n", ",") if "\n" in value else value
    print(f"{key}={summary}")


def main() -> int:
    force_all = os.environ.get("CI_FORCE_ALL", "").lower() in ("1", "true", "yes")
    rust_workspace = force_all or flag("rust_workspace")
    packages = [pkg for name, pkg, _ in PROJECTS if flag(name)]

    rust_all = rust_workspace or force_all
    rust_any = rust_all or bool(packages)

    frontend_dirs: list[str] = []
    if force_all or flag("frontend_all"):
        frontend_dirs = [d for _, d in FRONTENDS if Path(d, "package.json").is_file()]
    else:
        frontend_dirs = [d for name, d in FRONTENDS if flag(name) and Path(d, "package.json").is_file()]

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
