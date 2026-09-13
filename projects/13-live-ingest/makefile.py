#!/usr/bin/env python3
"""live-ingest — local dev task runner.

A wrapper around the day-to-day commands for this project (uv, the server, and
the probes that make a *live* server visible). The `Makefile` shells out to this
file so there is one source of truth with colors, emojis and readable output.
Help tables use `tools/makefile_help.py` (Rich — auto-installed from
`tools/requirements.txt`).

There is deliberately **no docker-compose** here: the source is a live RTMP
socket and everything downstream lives in a bounded in-memory window (see
SPEC.md), so this runner has no compose/db bundles.

The probes separate the ways "the player shows nothing" can happen, in order:

* `make planes`   — is anything up? The HTTP port and the RTMP port.
* `make publish`  — push a synthetic live stream at the RTMP port with ffmpeg.
* `make status`   — the server's view, including the last session failure,
                    which on a scaffold names the function to write next.
* `make streams`  — which keys are on air (did a publish get through V2?).
* `make playlist` — fetch the playlist for KEY (did V3 + V4 produce one?).
* `make smoke-rtmp` — the whole ingest path once, PASS/FAIL, with ffmpeg.

Usage:
    python3 makefile.py <task> [task ...]
    make <task>            # via the Makefile wrapper

Run `python3 makefile.py help` (or just `make`) to see every task.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR.parent.parent / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent.parent / "tools"))

from makefile_runner import (  # noqa: E402
    C,
    make_runner,
    register_dev_stack,
    register_help,
    register_md,
    register_python_checks,
    register_python_run,
    register_setup,
)

runner = make_runner(
    crate="live-ingest",
    help_title="🎥 live-ingest (RTMP → LL-HLS)",
    project_dir=PROJECT_DIR,
    # The HTTP delivery port. RTMP ingest is RTMP_PORT (1935), raw TCP.
    default_port="8080",
    help_footers=[
        ("Typical first run", "make setup && make sync && make run"),
        ("Push a live stream", "make publish   (needs ffmpeg)"),
        ("Prove the ingest path", "make smoke-rtmp"),
        ("Server + web player", "make dev"),
        ("Run all checks", "make verify"),
    ],
)

register_setup(runner)
register_python_checks(runner)
register_python_run(runner)
# web/ exists → full-stack `dev` (server + Vite on :5113) plus `web-install` / `frontend`.
register_dev_stack(runner, use_cargo_watch=False, vite_port="5113")


@runner.task("sync", "📦", "Setup", "Install/refresh the virtualenv from uv.lock")
def sync() -> None:
    runner.step("📦", "syncing dependencies…")
    runner.uv("sync")
    runner.ok("environment ready")


def _env() -> dict[str, str]:
    return runner.load_dotenv()


def _http_port() -> int:
    return int(_env().get("HTTP_PORT", runner.config.default_port))


def _rtmp_port() -> int:
    return int(_env().get("RTMP_PORT", "1935"))


def _key() -> str:
    if key := os.environ.get("KEY"):
        return key
    keys = [k.strip() for k in _env().get("STREAM_KEYS", "").split(",") if k.strip()]
    return keys[0] if keys else "testkey"


def _url(path: str = "") -> str:
    return f"http://localhost:{_http_port()}{path}"


def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _ffmpeg() -> str:
    local = Path.home() / ".local" / "bin" / "ffmpeg"
    found = str(local) if local.is_file() else shutil.which("ffmpeg")
    if not found:
        runner.fail("ffmpeg not found — install a static build to ~/.local/bin")
        sys.exit(1)
    return found


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


@runner.task("planes", "📡", "Probe", "Who is up: the HTTP delivery port and the RTMP port")
def planes() -> None:
    http_up = runner.port_open("127.0.0.1", _http_port())
    rtmp_up = runner.port_open("127.0.0.1", _rtmp_port())
    print()
    for up, name, port, note in (
        (http_up, "delivery (HTTP)", _http_port(), "LL-HLS playlists, parts, /metrics"),
        (rtmp_up, "ingest (RTMP)", _rtmp_port(), "raw TCP — broadcasters push here"),
    ):
        mark = f"{C.GREEN}●{C.RESET}" if up else f"{C.DIM}○{C.RESET}"
        state = "listening" if up else "closed"
        print(f"  {mark} {name:<17}:{port:<6} {state:<10} {C.DIM}{note}{C.RESET}")
    print()


@runner.task("status", "📊", "Probe", "GET /status — sessions, streams, the last session failure")
def status() -> None:
    code, body = _get(_url("/status"))
    if code != 200:
        runner.fail(f"unexpected status {code}: {body.strip()}")
        sys.exit(1)
    parsed = json.loads(body)
    print()
    print(f"  {C.BOLD}rtmp port{C.RESET}   {parsed['rtmp_port']}")
    print(f"  {C.BOLD}sessions{C.RESET}    {parsed['rtmp_sessions']} connected")
    print(f"  {C.BOLD}on air{C.RESET}      {parsed['live_streams']} streams")
    if parsed["open_ingest"]:
        print(f"  {C.BOLD}auth{C.RESET}        {C.YELLOW}OPEN — any key may publish{C.RESET}")
    print(
        f"  {C.BOLD}cadence{C.RESET}     part {parsed['target_part_secs']}s · "
        f"segment {parsed['target_segment_secs']}s · window {parsed['live_window_segments']}"
    )
    if parsed["last_session_failure"]:
        print(f"  {C.BOLD}last fail{C.RESET}   {C.YELLOW}{parsed['last_session_failure']}{C.RESET}")
        print(f"              {C.DIM}that is the worklist{C.RESET}")
    print()


@runner.task("streams", "📺", "Probe", "GET /live — the stream keys currently on air")
def streams() -> None:
    code, body = _get(_url("/live"))
    if code != 200:
        runner.fail(f"unexpected status {code}: {body.strip()}")
        sys.exit(1)
    keys = json.loads(body)["live"]
    if not keys:
        runner.warn("nothing on air — a publish has to get through V1 and V2 first")
        return
    for key in keys:
        print(f"  {C.GREEN}●{C.RESET} {key}   {C.DIM}{_url(f'/live/{key}/index.m3u8')}{C.RESET}")


@runner.task(
    "playlist", "🧾", "Probe", "GET the LL-HLS playlist for KEY (default: first STREAM_KEYS)"
)
def playlist() -> None:
    key = _key()
    code, body = _get(_url(f"/live/{key}/index.m3u8"))
    runner.step("🧾", f"GET /live/{key}/index.m3u8 → {code}")
    print(body)


@runner.task("metrics", "📈", "Probe", "GET /metrics — the live_ingest_* series")
def metrics() -> None:
    _, body = _get(_url("/metrics"))
    lines = [line for line in body.splitlines() if line.startswith("live_ingest_")]
    if not lines:
        runner.warn("no live_ingest_* series yet — is the server running?")
        return
    for line in lines:
        print(f"  {line}")


@runner.task("smoke", "🔥", "Run", "Hit /healthz on HTTP_PORT (server must be running)")
def smoke() -> None:
    runner.require("curl", "Install curl to use this target.")
    runner.step("🔥", f"GET {_url('/healthz')}")
    rc = runner.run(["curl", "-sf", _url("/healthz")], check=False)
    print()
    if rc == 0:
        runner.ok("healthz OK")
    else:
        runner.fail("healthz failed — is the server running?")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Driving a broadcaster
# --------------------------------------------------------------------------- #


@runner.task("publish", "🎬", "Run", "Push a synthetic 720p30 stream to rtmp://…/live/KEY (Ctrl-C)")
def publish() -> None:
    """A broadcaster with no media file: ffmpeg's test pattern + a sine tone.

    `-g 60` puts a keyframe every 2 s at 30 fps, so segments (which must start on
    one) have somewhere to cut. The server must be running.
    """
    key = _key()
    target = f"rtmp://127.0.0.1:{_rtmp_port()}/live/{key}"
    runner.step("🎬", f"publishing → {target}   {C.DIM}(Ctrl-C to stop){C.RESET}")
    print(f"   {C.DIM}then: make streams · make playlist · open the web player (make dev){C.RESET}")
    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "warning", "-re",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "flv", target,
    ]  # fmt: skip
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print()
        runner.warn("stopped publishing")


@runner.task("smoke-rtmp", "📡", "Run", "End-to-end RTMP test: start server, push ffmpeg, report")
def smoke_rtmp() -> None:
    # Self-contained: the script starts and stops its own server on scratch ports.
    script = runner.project_dir / "scripts" / "smoke_rtmp.py"
    runner.step("📡", "running the RTMP ingest smoke test…")
    rc = runner.run([sys.executable, str(script)], cwd=runner.project_dir, check=False)
    if rc != 0:
        sys.exit(rc)


# --------------------------------------------------------------------------- #
# The boss fight
# --------------------------------------------------------------------------- #


@runner.task("bench", "🐉", "Bench", "The Latency Wall: 10 min 1080p30 + ≥200 LL-HLS players")
def bench() -> None:
    """The boss fight's harness — yours to build in `bench/`.

    Three things to decide before writing a line. Latency is *glass-to-glass*:
    burn a timecode into the source and read it back from what a player renders,
    because server-side timestamps cannot see the player's buffer. "≥95% served
    first time" needs the load generator to record, per request, whether the
    part it asked for was in the first response. And "RSS stays flat" is a
    ten-minute claim — sample it throughout, not once at the end.
    """
    runner.warn("bench/ is yours to build — see the 🐉 Boss fight section of SPEC.md")
    print(f"   {C.DIM}make md       # read the Arena + 'the boss falls when' lines{C.RESET}")
    print(f"   {C.DIM}make publish  # a broadcaster to point it at{C.RESET}")
    print(f"   {C.DIM}while it runs: make metrics | grep -E 'hold|reloads|edge_age'{C.RESET}")


@runner.task("profile", "🔥", "Bench", "Sample the running server with py-spy (10s flamegraph)")
def profile() -> None:
    """The Definition-of-done profiling gate.

    py-spy attaches to a *running* process, so start the server, publish and
    drive players at it, and sample while that is happening — a flamegraph of
    an idle loop tells you nothing. What to look for is named in `main.py`.
    """
    out = runner.project_dir / "docs" / "flamegraph.svg"
    pid = os.environ.get("PID")
    if not pid:
        runner.fail("set PID=<server pid> — py-spy samples a running process")
        print(f"   {C.DIM}e.g. `PID=$(pgrep -f live-ingest) make profile`{C.RESET}")
        sys.exit(1)
    runner.step("🔥", "sampling for 10s — drive traffic meanwhile")
    runner.uv("run", "py-spy", "record", "--duration", "10", "--pid", pid, "--output", str(out))
    runner.ok(f"wrote {out}")


register_md(runner)
register_help(runner)

if __name__ == "__main__":
    runner.entrypoint(sys.argv[1:])
