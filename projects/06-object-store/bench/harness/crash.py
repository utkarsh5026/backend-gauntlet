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
   precisely what we are *not* testing — at a series of moments swept across
   the upload window, so the kill lands both before and after the commit.
4. Restart it over the same data dir.
5. Assert every blob under `objects/` still hashes to its own name, and that the
   key either reads back byte-exact or 404s.

Both outcomes must actually occur, and the script fails if they don't. A sweep
that lands every kill on one side of the commit proves only half the claim, and
it looks identical to a passing run — which is how a fixed `KILL_AFTER` quietly
tests nothing.

What must never appear is a third outcome: a `200` whose bytes do not match the
digest they are stored under. That is the failure the temp → fsync → rename →
fsync-dir sequence exists to make impossible, and a truncated blob is
undetectable to every reader afterwards because the *name* still looks right.

    uv run python bench/harness/crash.py
    SIZE_MB=256 FRACTIONS=0.1,0.5,0.9,1.5 uv run python bench/harness/crash.py
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


def store_command() -> list[str]:
    """How to launch the store as a process we can actually signal.

    The console script directly, **not** `uv run object-store`. This cost a
    debugging round: `uv run` is a wrapper process, so a signal sent to it goes
    to the wrapper while the real server keeps running and keeps holding the
    port. The next attempt's `start_store` then sees a healthy `/healthz` from
    the *old* server, hands back the new process object, and the upload lands on
    a socket that is about to close — "connection reset by peer", from a test
    that was supposed to be measuring crash safety.

    It is exactly the trap the Dockerfile's `ENTRYPOINT` comment describes:
    whatever receives the signal has to be the thing you meant to signal.
    """
    # Next to the interpreter running this script, not under `ROOT`: the uv
    # workspace's venv lives at the *repo* root, two levels above this project.
    # Looking for it under `ROOT` was itself a bug — the path never matched, the
    # `uv run` fallback was taken every time, and every "killed" store leaked a
    # surviving child that went on holding the port.
    console = Path(sys.executable).parent / "object-store"
    if console.is_file():
        return [str(console)]
    return ["uv", "run", "object-store"]


def start_store(data_dir: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ, DATA_DIR=str(data_dir), PORT=str(PORT), LOG_LEVEL="warning")
    process = subprocess.Popen(
        store_command(),
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


def stop_store(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM, then wait for the port to actually be free.

    Waiting on the process is not enough on its own — see `store_command` — so
    this also confirms nothing is still answering before returning. Otherwise
    the next attempt races a listener that has not finished closing.
    """
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=40)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    wait_for_port_free()


def wait_for_port_free() -> None:
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=1):
                time.sleep(0.25)
        except (urllib.error.URLError, OSError):
            return
    raise RuntimeError(f"something is still serving on :{PORT}")


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


UPLOADER = """
import sys, urllib.request
path, url = sys.argv[1], sys.argv[2]
body = open(path, 'rb').read()
# Announce *after* the payload is in memory and *before* the request goes out,
# so the parent's clock covers the HTTP window and nothing else.
sys.stdout.write('GO\\n')
sys.stdout.flush()
urllib.request.urlopen(urllib.request.Request(url, data=body, method='PUT'), timeout=120)
sys.stdout.write('DONE\\n')
sys.stdout.flush()
"""
"""The child that does the uploading, so the parent is free to do the killing.

It reads the payload from a **file** rather than a pipe. Piping 64 MiB into the
child's stdin puts the parent's write and the child's read inside the window the
sweep is trying to measure, which is how the first version of this harness ended
up with fractions that meant nothing.
"""


def spawn_uploader(payload_path: Path) -> subprocess.Popen[str]:
    """Start the uploader and block until it is about to issue the request."""
    child = subprocess.Popen(
        [sys.executable, "-c", UPLOADER, str(payload_path), f"{BASE}/crash/object"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    if child.stdout.readline().strip() != "GO":
        raise RuntimeError(f"uploader never signalled GO: {_child_stderr(child)}")
    return child


def _child_stderr(child: subprocess.Popen[str]) -> str:
    """Whatever the uploader complained about before dying."""
    if child.stderr is None:
        return "(no stderr captured)"
    try:
        return child.stderr.read().strip()[-500:] or "(silent)"
    except (ValueError, OSError):
        return "(stderr unreadable)"


def time_one_upload(data_dir: Path, payload_path: Path) -> float:
    """How long an uninterrupted PUT takes, measured the way an attempt runs it.

    Through the same subprocess, timed from the same `GO` marker. Measuring it
    any other way makes the fractions below lie: an in-process baseline is
    faster than the child's real timeline, so every kill lands early, every run
    reports "nothing", and the harness looks like it passes while testing half
    the claim.
    """
    process = start_store(data_dir)
    try:
        request("PUT", "/crash")
        child = spawn_uploader(payload_path)
        assert child.stdout is not None
        started = time.perf_counter()
        if child.stdout.readline().strip() != "DONE":
            raise RuntimeError(f"baseline upload did not finish: {_child_stderr(child)}")
        elapsed = time.perf_counter() - started
        child.wait(timeout=10)
        return elapsed
    finally:
        stop_store(process)


def main() -> None:
    size_mb = int(os.environ.get("SIZE_MB", "64"))
    payload = os.urandom(size_mb * 1024 * 1024)
    expected = hashlib.md5(payload).hexdigest()

    scratch = Path(tempfile.mkdtemp(prefix="crash-test-"))
    payload_path = scratch / "payload.bin"
    payload_path.write_bytes(payload)
    data_dir = scratch / "data"
    data_dir.mkdir()

    survived = 0
    truncated = 0
    outcomes = {"nothing": 0, "whole": 0}

    try:
        baseline = time_one_upload(data_dir, payload_path)
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"baseline upload: {baseline * 1000:.0f} ms ({size_mb} MiB)\n")

        # Sweep the kill across the upload window rather than firing at one
        # fixed moment. A single timing is a much weaker test than it looks:
        # land it consistently early and *every* run takes the "nothing" branch,
        # so the "whole object" half is never exercised and a bug that only
        # appears after the commit goes unseen.
        #
        # The last entry is `None`, and it is the important one. Sleeping some
        # multiple of a baseline sample is a race: the run being killed is a
        # *different* upload whose commit can land anywhere, so a fraction that
        # produced "whole" on one run produces "nothing" on the next — which is
        # exactly what this harness did before. `None` means "wait for the child
        # to report DONE, then kill". The PUT has returned, so the object is
        # provably committed and indexed, and the post-commit case stops
        # depending on luck.
        raw = os.environ.get("FRACTIONS")
        schedule: list[float | None] = (
            [float(f) for f in raw.split(",")] if raw else [0.2, 0.5, 0.8, 1.2]
        )
        schedule.append(None)

        for attempt, fraction in enumerate(schedule, start=1):
            process = start_store(data_dir)
            request("PUT", "/crash")
            child = spawn_uploader(payload_path)

            if fraction is None:
                assert child.stdout is not None
                if child.stdout.readline().strip() != "DONE":
                    raise RuntimeError(f"uploader did not finish its PUT: {_child_stderr(child)}")
                kill_after = 0.0
            else:
                kill_after = baseline * fraction
                time.sleep(kill_after)
            # SIGKILL, not SIGTERM: a graceful shutdown is what we are *not*
            # testing. This is power loss, and the process gets no chance to
            # finish a rename or flush a directory entry.
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=10)
            child.kill()
            wait_for_port_free()

            label = (
                f"attempt {attempt} (after the PUT returned)"
                if fraction is None
                else f"attempt {attempt} ({fraction:.2f}x = {kill_after * 1000:.0f} ms)"
            )

            broken = audit_blobs(data_dir)
            if broken:
                truncated += 1
                print(f"  {label}: FAIL corrupt blob(s): {broken}")
                continue

            process = start_store(data_dir)
            status, body = request("GET", "/crash/object")
            stop_store(process)

            if status == 404 and fraction is not None:
                print(f"  {label}: ok  nothing (crashed before the commit)")
                outcomes["nothing"] += 1
                survived += 1
            elif status == 200 and hashlib.md5(body).hexdigest() == expected:
                print(f"  {label}: ok  whole object ({len(body)} bytes)")
                outcomes["whole"] += 1
                survived += 1
            elif fraction is None:
                # The PUT returned 200 before the kill, so the object *must* be
                # readable afterwards. Anything else here is lost acknowledged
                # data, which is worse than a truncated blob.
                truncated += 1
                print(f"  {label}: FAIL status={status} - LOST AN ACKNOWLEDGED WRITE")
            else:
                truncated += 1
                print(f"  {label}: FAIL status={status} len={len(body)} - TRUNCATED")

            shutil.rmtree(data_dir, ignore_errors=True)
            data_dir.mkdir(parents=True, exist_ok=True)

        total = len(schedule)
        print(
            f"\n{survived}/{total} all-or-nothing "
            f"({outcomes['nothing']} nothing, {outcomes['whole']} whole), "
            f"{truncated} truncated"
        )
        if truncated:
            print("A truncated object means the commit sequence is broken. This is")
            print("silent data loss: the blob's name still looks correct forever.")
            sys.exit(1)
        if not outcomes["whole"]:
            # The final attempt kills only after the PUT returned, so a
            # committed object must be readable. Reaching here means it was not.
            print("The post-commit attempt did not return the object. An")
            print("acknowledged write did not survive a hard kill.")
            sys.exit(1)
        if not outcomes["nothing"]:
            # Not a failure of the store — a failure of coverage. Every swept
            # kill landed after the commit, so the mid-stream half was never
            # exercised. Lower FRACTIONS or raise SIZE_MB.
            print("No kill landed mid-upload, so the interrupted case was never")
            print("tested. Lower FRACTIONS or raise SIZE_MB and re-run.")
            sys.exit(1)
        print("V1's atomic commit holds: every crash left the whole object or none.")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
