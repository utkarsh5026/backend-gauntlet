#!/usr/bin/env python3
"""The crash test — `kill -9` mid-PUT, then read back.

The one claim in this project that cannot be proved in-process: V1 says a crash
during a write leaves **either** the whole object **or** nothing, never a
truncated blob under its final name. Asserting that needs a real process to
kill, at a real moment, with a real page cache in play.

The shape:

1. Start the store as a subprocess on a scratch data dir.
2. Begin a large PUT and let it get well underway.
3. `SIGKILL` the process — not `SIGTERM`, because a graceful shutdown is
   precisely what we are *not* testing.
4. Restart it over the same data dir.
5. Assert every blob under `objects/` still hashes to its own name, and that the
   key either reads back byte-exact or 404s.

What must never appear is a third outcome: a `200` whose bytes do not match the
digest they are stored under. That is the failure the temp → fsync → rename →
fsync-dir sequence exists to make impossible, and a truncated blob is
undetectable to every reader afterwards because the *name* still looks right.

    uv run python bench/harness/crash.py
    ITERATIONS=10 KILL_AFTER=0.3 uv run python bench/harness/crash.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("CRASH_PORT", "9207"))
BASE = f"http://127.0.0.1:{PORT}"


def start_store(data_dir: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ, DATA_DIR=str(data_dir), PORT=str(PORT), LOG_LEVEL="warning")
    process = subprocess.Popen(
        ["uv", "run", "object-store"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError("store exited during startup")
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=1):
                return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise RuntimeError("store never became healthy")


def request(method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def audit_blobs(data_dir: Path) -> list[str]:
    """Every blob whose bytes no longer hash to its own filename.

    This is the whole assertion. A truncated blob is not detectable by size or
    by opening it — only by re-deriving the content address, which is exactly
    what content addressing buys.
    """
    broken: list[str] = []
    for path in (data_dir / "objects").rglob("*"):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != path.name:
            broken.append(f"{path.name} hashes to {digest}")
    return broken


def main() -> None:
    iterations = int(os.environ.get("ITERATIONS", "5"))
    kill_after = float(os.environ.get("KILL_AFTER", "0.4"))
    payload = os.urandom(64 * 1024 * 1024)
    expected = hashlib.md5(payload).hexdigest()

    data_dir = Path(tempfile.mkdtemp(prefix="crash-test-"))
    survived = 0
    truncated = 0

    try:
        for attempt in range(1, iterations + 1):
            process = start_store(data_dir)
            request("PUT", "/crash")

            # Kick the upload off in a child so this process can do the killing.
            uploader = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys,urllib.request;"
                    "d=sys.stdin.buffer.read();"
                    f"r=urllib.request.Request('{BASE}/crash/object',data=d,method='PUT');"
                    "urllib.request.urlopen(r,timeout=60)",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert uploader.stdin is not None
            try:
                uploader.stdin.write(payload)
            except BrokenPipeError:
                pass

            time.sleep(kill_after)
            # SIGKILL, not SIGTERM: a graceful shutdown is what we are *not*
            # testing. This is power loss, and the process gets no chance to
            # finish a rename or flush a directory entry.
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=10)
            uploader.kill()

            broken = audit_blobs(data_dir)
            if broken:
                truncated += 1
                print(f"  attempt {attempt}: ❌ corrupt blob(s): {broken}")
                continue

            process = start_store(data_dir)
            status, body = request("GET", "/crash/object")
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)

            if status == 404:
                print(f"  attempt {attempt}: ✅ nothing (crashed before the commit)")
                survived += 1
            elif status == 200 and hashlib.md5(body).hexdigest() == expected:
                print(f"  attempt {attempt}: ✅ whole object ({len(body)} bytes)")
                survived += 1
            else:
                truncated += 1
                print(f"  attempt {attempt}: ❌ status={status} len={len(body)} — TRUNCATED")

            shutil.rmtree(data_dir, ignore_errors=True)
            data_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{survived}/{iterations} all-or-nothing, {truncated} truncated")
        if truncated:
            print("A truncated object means the commit sequence is broken. This is")
            print("silent data loss: the blob's name still looks correct forever.")
            sys.exit(1)
        print("V1's atomic commit holds: every crash left the whole object or none of it.")
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
