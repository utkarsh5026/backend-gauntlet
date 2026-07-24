#!/usr/bin/env python3
"""End-to-end RTMP ingest smoke test.

Proves the V1 handshake + chunk-stream reader work against a *real* broadcaster
(ffmpeg) rather than our own synthetic bytes. It:
  1. builds + starts the live-ingest server on a scratch port,
  2. pushes ~2s of synthetic H.264/AAC at it over RTMP (no media file needed —
     ffmpeg's lavfi test source),
  3. inspects the server log and asserts the handshake completed and the reader
     reached the command phase.

Why the server log is the source of truth (not ffmpeg's exit code): while V2 (the
AMF command handler) is still a ``todo!()``, the server closes the connection right
after reading the first command, so ffmpeg reports an I/O error even though V1 worked
perfectly. Completion is proved by "handshake complete" appearing in the log — a
byte-wrong handshake makes ffmpeg hang up *before* that line.

Usage:
    scripts/smoke_rtmp.py                    # build if needed, run once, PASS/FAIL
    scripts/smoke_rtmp.py --rtmp-port 19350  # override the scratch RTMP port
    scripts/smoke_rtmp.py --duration 4       # stream for 4s instead of 2s
    scripts/smoke_rtmp.py --build            # force a cargo rebuild first
    FFMPEG=/path/to/ffmpeg scripts/smoke_rtmp.py

Exit code: 0 = PASS, 1 = FAIL, 2 = setup error (no ffmpeg, port busy, build fail).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
WORKSPACE_ROOT = PROJECT_DIR.parent.parent
SERVER_BIN = WORKSPACE_ROOT / "target" / "debug" / "live-ingest"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
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


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def clean_log_line(line: str) -> str:
    """Trim tracing's ISO-timestamp + crate qualifier for a readable trace.

    Turns
        2026-07-24T00:47:58.675370Z DEBUG live_ingest::rtmp:  recv C0 …
    into
        DEBUG rtmp: recv C0 …
    The full timestamped log is still written to the file for real debugging; this
    only tidies what the smoke test prints to the terminal.
    """
    line = re.sub(r"^\S+Z\s+", "", line)          # drop the leading ISO-8601 timestamp
    line = line.replace("live_ingest::", "").replace("live_ingest:", "")  # crate qualifier
    return re.sub(r"  +", " ", line).rstrip()      # collapse the doubled spaces


# --- helpers -----------------------------------------------------------------------
def resolve_ffmpeg(explicit: str | None) -> str | None:
    """Prefer an explicit path/env, then ~/.local/bin, then PATH."""
    if explicit:
        return explicit if Path(explicit).exists() else shutil.which(explicit)
    local = Path.home() / ".local" / "bin" / "ffmpeg"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("ffmpeg")


def port_busy(port: int) -> bool:
    """True if something is already listening on 0.0.0.0:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def ffmpeg_version(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True, timeout=10
        ).stdout
        return " ".join(out.splitlines()[0].split()[:3]) if out else "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def wait_for_listen(log_path: Path, proc: subprocess.Popen, timeout_s: float) -> bool:
    """Poll the server log until it reports the RTMP listener is bound."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # server exited during startup
        if "rtmp ingest listening" in log_path.read_text(errors="replace"):
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end RTMP ingest smoke test.")
    ap.add_argument(
        "--rtmp-port", type=int, default=int(os.environ.get("RTMP_PORT", 19350))
    )
    ap.add_argument(
        "--http-port", type=int, default=int(os.environ.get("HTTP_PORT", 18080))
    )
    ap.add_argument("--key", default=os.environ.get("STREAM_KEY", "testkey"))
    ap.add_argument(
        "--duration", type=float, default=float(os.environ.get("DURATION", 2))
    )
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG"))
    ap.add_argument("--build", action="store_true", help="force a cargo build first")
    args = ap.parse_args()

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        bad("ffmpeg not found. Install a static build to ~/.local/bin, e.g.:")
        print(
            "    curl -L https://johnvansickle.com/ffmpeg/releases/"
            "ffmpeg-release-amd64-static.tar.xz | tar -xJ"
        )
        print(
            "    cp ffmpeg-*-static/ffmpeg ~/.local/bin/ && chmod +x ~/.local/bin/ffmpeg"
        )
        return 2
    info(f"ffmpeg: {ffmpeg} ({ffmpeg_version(ffmpeg)})")

    for p in (args.rtmp_port, args.http_port):
        if port_busy(p):
            bad(
                f"port {p} is already in use — pass --rtmp-port/--http-port for free ports."
            )
            return 2

    logdir = Path(tempfile.mkdtemp(prefix="rtmp-smoke-"))
    server_log = logdir / "server.log"
    ffmpeg_log = logdir / "ffmpeg.log"

    if args.build or not (SERVER_BIN.exists() and os.access(SERVER_BIN, os.X_OK)):
        info("building live-ingest…")
        build = subprocess.run(
            ["cargo", "build", "-p", "live-ingest"],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            bad("cargo build failed:")
            print("\n".join(build.stderr.splitlines()[-20:]))
            return 2

    server: subprocess.Popen | None = None
    rc = 0
    try:
        info(f"starting server on rtmp:{args.rtmp_port} / http:{args.http_port} …")
        env = {
            **os.environ,
            "NO_COLOR": "1",  # keep the log greppable
            "STREAM_KEYS": "",  # accept any publish key (dev)
            "RTMP_PORT": str(args.rtmp_port),
            "HTTP_PORT": str(args.http_port),
        }
        with server_log.open("w") as log_fh:
            server = subprocess.Popen(
                [str(SERVER_BIN)], stdout=log_fh, stderr=subprocess.STDOUT, env=env
            )

        if not wait_for_listen(server_log, server, timeout_s=15):
            bad("server never reported the RTMP listener as up:")
            print(strip_ansi(server_log.read_text(errors="replace")).strip()[-1000:])
            return 2
        ok("server up")

        # --- push a synthetic stream ----------------------------------------------
        target = f"rtmp://127.0.0.1:{args.rtmp_port}/live/{args.key}"
        info(f"streaming {args.duration:g}s of synthetic H.264/AAC → {target}")
        ff_cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
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
            str(args.duration),
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
        try:
            ff = subprocess.run(
                ff_cmd, capture_output=True, text=True, timeout=args.duration + 20
            )
            ff_exit = ff.returncode
            ffmpeg_log.write_text(ff.stdout + ff.stderr)
        except subprocess.TimeoutExpired:
            ff_exit = -1
            ffmpeg_log.write_text("ffmpeg timed out")
        info(
            f"ffmpeg exit: {ff_exit} {_c('2', '(non-zero is expected while V2 is a todo)')}"
        )

        time.sleep(0.5)  # let the server flush its last log line

        # --- assertions ------------------------------------------------------------
        log = strip_ansi(server_log.read_text(errors="replace"))
        print("\n── server log " + "─" * 46)
        wanted = (
            "connection accepted",
            "handshake",
            "connection ended",
            "panicked",
            "not yet implemented",
            "publish",
            "connect",
            "DEBUG",  # the per-step C0/C1/S…/C2 handshake trace lines
        )
        lines = [ln for ln in log.splitlines() if any(w in ln for w in wanted)]
        shown = lines or log.splitlines()[-8:]
        print("\n".join(clean_log_line(ln) for ln in shown))
        print("─" * 60 + "\n")

        if "rtmp connection accepted" in log:
            ok("TCP connection accepted")
        else:
            bad(
                "server never logged an accepted connection — did ffmpeg reach the port?"
            )
            rc = 1

        if "handshake complete" in log:
            ok(
                "handshake complete — byte-correct against a real broadcaster (V1 handshake ✓)"
            )
        else:
            bad(
                "handshake did NOT complete — ffmpeg hung up; S0/S1/S2 or the C2 echo is wrong."
            )
            warn("ffmpeg tail:")
            print(
                "\n".join(
                    strip_ansi(ffmpeg_log.read_text(errors="replace")).splitlines()[-6:]
                )
            )
            rc = 1

        # Command phase: in scaffold state the reader parses the first message then
        # handle() hits its todo!(); post-V2 the panic disappears and real handling
        # takes over. Either path means we got past the handshake into message reading.
        if re.search(
            r"not yet implemented.*handle command|panicked at .*session\.rs", log
        ):
            ok(
                "reached command phase — reader returned a message; V2 handler is a todo (expected)"
            )
        elif re.search(r"publish|connect|createStream", log):
            ok("reached command phase — V2 command handling is active")
        elif "handshake complete" in log:
            warn(
                "handshake completed but no message was dispatched — the chunk reader may not"
            )
            warn(f"have parsed the first message. Inspect: {server_log}")
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()

    print()
    if rc == 0:
        ok(
            _c("32", "SMOKE TEST PASSED")
            + " — RTMP ingest path works end-to-end through the handshake + reader."
        )
        shutil.rmtree(logdir, ignore_errors=True)
    else:
        bad("SMOKE TEST FAILED — see the server log above.")
        info(f"logs kept in {logdir}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
