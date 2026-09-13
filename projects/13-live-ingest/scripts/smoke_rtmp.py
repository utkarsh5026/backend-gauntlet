#!/usr/bin/env python3
"""End-to-end RTMP ingest smoke test.

Proves the ingest path against a *real* broadcaster (ffmpeg) rather than our own
synthetic bytes. It:
  1. starts the live-ingest server (`uv run live-ingest`) on scratch ports,
  2. pushes a few seconds of synthetic H.264/AAC at it over RTMP (no media file
     needed — ffmpeg's lavfi test sources),
  3. reads the server's JSON log and reports how far the connection got.

Why the server log is the source of truth, not ffmpeg's exit code: until V2 is
built the server closes the connection right after the handshake or the first
command, so ffmpeg always reports an I/O error — even when V1 is perfect. A
byte-wrong handshake makes ffmpeg hang up *before* "handshake complete" is
logged, so that line appearing is the proof.

Milestones, in the order a working build reaches them:
  * "rtmp connection accepted"     the listener is up and ffmpeg reached it
  * "handshake complete"           V1's handshake is byte-correct   (gates PASS)
  * "rtmp session reached an unbuilt vertical"
                                   names the function to write next
  * "publish accepted"             V2's state machine let ffmpeg publish, if you
                                   log that phrase when it does (reported only)

Usage:
    scripts/smoke_rtmp.py                    # run once, PASS/FAIL
    scripts/smoke_rtmp.py --rtmp-port 19350  # override the scratch RTMP port
    scripts/smoke_rtmp.py --duration 4       # stream for 4s instead of 2s
    FFMPEG=/path/to/ffmpeg scripts/smoke_rtmp.py

Exit code: 0 = PASS, 1 = FAIL, 2 = setup error (no ffmpeg/uv, port busy).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_c('2', '·')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('32', '✔')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('33', '!')} {msg}")


def bad(msg: str) -> None:
    print(_c("31", f"x {msg}"))


def resolve_ffmpeg(explicit: str | None) -> str | None:
    """Prefer an explicit path/env, then ~/.local/bin, then PATH."""
    if explicit:
        return explicit if Path(explicit).exists() else shutil.which(explicit)
    local = Path.home() / ".local" / "bin" / "ffmpeg"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("ffmpeg")


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def read_events(log_path: Path) -> list[dict[str, Any]]:
    """The server's structlog output, one JSON object per line.

    stderr is redirected to a file, so `common_telemetry` picks JSON — which is
    what makes this parseable instead of grep-able.
    """
    events: list[dict[str, Any]] = []
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            # structlog writes string keys; the cast states what json.loads of
            # our own log line already guarantees.
            events.append(cast(dict[str, Any], parsed))
    return events


def logged(events: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == event]


def wait_for_listen(log_path: Path, proc: subprocess.Popen[bytes], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if logged(read_events(log_path), "rtmp ingest listening"):
            return True
        time.sleep(0.2)
    return False


def ffmpeg_command(ffmpeg: str, target: str, duration: float) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "flv",
        target,
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end RTMP ingest smoke test.")
    ap.add_argument("--rtmp-port", type=int, default=int(os.environ.get("SMOKE_RTMP_PORT", 19350)))
    ap.add_argument("--http-port", type=int, default=int(os.environ.get("SMOKE_HTTP_PORT", 18080)))
    ap.add_argument("--key", default=os.environ.get("STREAM_KEY", "testkey"))
    ap.add_argument("--duration", type=float, default=float(os.environ.get("DURATION", 2)))
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG"))
    args = ap.parse_args()

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        bad("ffmpeg not found. Install a static build to ~/.local/bin, e.g.:")
        print(
            "    curl -L https://johnvansickle.com/ffmpeg/releases/"
            "ffmpeg-release-amd64-static.tar.xz | tar -xJ"
        )
        print("    cp ffmpeg-*-static/ffmpeg ~/.local/bin/ && chmod +x ~/.local/bin/ffmpeg")
        return 2
    if shutil.which("uv") is None:
        bad("uv not found — see https://docs.astral.sh/uv/")
        return 2
    info(f"ffmpeg: {ffmpeg}")

    for port in (args.rtmp_port, args.http_port):
        if port_busy(port):
            bad(f"port {port} is already in use — pass --rtmp-port/--http-port for free ports.")
            return 2

    logdir = Path(tempfile.mkdtemp(prefix="rtmp-smoke-"))
    server_log = logdir / "server.log"
    env = {
        **os.environ,
        "STREAM_KEYS": "",  # accept any publish key (dev)
        "RTMP_PORT": str(args.rtmp_port),
        "HTTP_PORT": str(args.http_port),
        "LOG_LEVEL": "debug",
    }

    server: subprocess.Popen[bytes] | None = None
    rc = 0
    try:
        info(f"starting server on rtmp:{args.rtmp_port} / http:{args.http_port} …")
        with server_log.open("wb") as log_fh:
            server = subprocess.Popen(
                ["uv", "run", "live-ingest"],
                cwd=PROJECT_DIR,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
        if not wait_for_listen(server_log, server, timeout_s=30):
            bad("server never reported the RTMP listener as up:")
            print(server_log.read_text(errors="replace")[-1500:])
            return 2
        ok("server up")

        target = f"rtmp://127.0.0.1:{args.rtmp_port}/live/{args.key}"
        info(f"streaming {args.duration:g}s of synthetic H.264/AAC → {target}")
        try:
            ff = subprocess.run(
                ffmpeg_command(ffmpeg, target, args.duration),
                capture_output=True,
                text=True,
                timeout=args.duration + 20,
            )
            ff_exit = ff.returncode
        except subprocess.TimeoutExpired:
            ff_exit = -1
        note = "published + streamed" if ff_exit == 0 else "connection dropped before the end"
        info(f"ffmpeg exit: {ff_exit} {_c('2', f'({note})')}")
        time.sleep(0.5)

        events = read_events(server_log)
        print("\n── server events " + "─" * 43)
        for event in events:
            name = str(event.get("event", ""))
            if name.startswith(("rtmp", "handshake", "publish")):
                extra = {k: v for k, v in event.items() if k not in ("event", "timestamp", "level")}
                print(f"  {event.get('level', ''):<7} {name}  {_c('2', json.dumps(extra))}")
        print("─" * 60 + "\n")

        if logged(events, "rtmp connection accepted"):
            ok("TCP connection accepted")
        else:
            bad("server never logged an accepted connection — did ffmpeg reach the port?")
            rc = 1

        if logged(events, "handshake complete"):
            ok("handshake complete — byte-correct against a real broadcaster (V1 handshake ✓)")
        else:
            bad("handshake did NOT complete — ffmpeg hung up, or V1 is still unbuilt.")
            rc = 1

        for failure in logged(events, "rtmp session reached an unbuilt vertical"):
            warn(f"worklist: {failure.get('error')}")

        if logged(events, "publish accepted"):
            ok("publish accepted — connect → createStream → publish completed (V2 ✓)")
            if ff_exit != 0:
                warn(
                    f"publish was accepted but ffmpeg exited {ff_exit} — "
                    "a later reply may be malformed"
                )
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    print()
    if rc == 0:
        ok(_c("32", "SMOKE TEST PASSED") + " — the RTMP ingest path works through the handshake.")
        shutil.rmtree(logdir, ignore_errors=True)
    else:
        bad("SMOKE TEST FAILED — see the server events above.")
        info(f"logs kept in {logdir}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
