"""`ffmpeg` / `ffprobe` plumbing — the one place this project shells out.

**Fully wired**, on purpose: rebuilding an H.264 encoder is not the exercise. What
*is* the exercise is everything around the encoder — *where* to cut (V1), how to
schedule the cuts (V2), running them in parallel and idempotently (V3), and how to
glue the results back seamlessly (V4). So the actual `-c:v libx264 …` invocation is
a subprocess call through `run`; the orchestration is yours.

`run` executes a command and turns a failure into a `TranscodeToolError` carrying
stderr. `probe_keyframes` / `probe_duration` are the inputs V1 plans against.

## An argv list, never a shell string

Every call goes through `asyncio.create_subprocess_exec(bin, *args)`: the kernel
receives each argument as one inert string and no shell ever parses it, so a source
named `bbb.mp4; rm -rf ~` is a file that doesn't exist rather than a command that
runs. The tempting alternatives each bring the shell back —
`create_subprocess_shell`, `subprocess.run(..., shell=True)`, `os.system`, or an
f-string handed to `sh -c`. Build lists; never format command lines.

## Not blocking the loop

`subprocess.run` would block the event loop for the whole encode: every other
worker, the scheduler and every HTTP request would stop for minutes. The asyncio
subprocess API waits on the child without holding the loop, which is what lets N
workers drive N encodes from one Python thread. The CPU work happens inside
ffmpeg's processes, where the GIL never sees it.

## A cancelled call kills its child

If the coroutine awaiting a command is cancelled — shutdown's drain budget ran out
— the child is killed and reaped before the cancellation propagates. Otherwise the
ffmpeg process outlives the task that owned it: still burning a core, still writing
a temp file, with nobody left to settle its task.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from os import PathLike

from .errors import TranscodeToolError

__all__ = ["probe_duration", "probe_keyframes", "run"]

type Arg = str | PathLike[str]

STDERR_TAIL_CHARS = 4096
"""How much of a failed command's stderr is kept. ffmpeg prints its banner and
build configuration *before* the actual error, so the end is the part that
matters, and an unbounded stderr would land whole in `tasks.last_error`."""


async def _exec(bin_: str, args: Sequence[Arg]) -> bytes:
    """Run `bin_` to completion and return its stdout.

    Raises `TranscodeToolError` if it can't be spawned or exits non-zero.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_,
            *args,
            # ffmpeg reads stdin for interactive keys; a closed stdin means a
            # stray `q` in the parent's terminal can never stop an encode.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # Not found, not executable: the binary never started.
        raise TranscodeToolError(f"spawn `{bin_}`: {exc}") from exc

    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-STDERR_TAIL_CHARS:]
        raise TranscodeToolError(f"`{bin_}` exited {proc.returncode}: {detail}")
    return stdout


async def run(bin_: str, args: Sequence[Arg]) -> None:
    """Run an external command to completion, capturing stderr on failure.

    `args` is passed as-is: the caller builds the full argument vector. This is
    where V3's *deterministic* encode flags and V4's remux are assembled.
    """
    await _exec(bin_, args)


async def probe_keyframes(ffprobe: str, source: Arg) -> list[float]:
    """Timestamps (seconds) of every **keyframe** in the source's first video
    stream, in the order ffprobe reports them.

    This is the raw material V1 turns into a chunk plan: a source may only be cut
    into independently transcodable chunks *at* these points. `-skip_frame nokey`
    makes the decoder report keyframes only.
    """
    out = await _exec(
        ffprobe,
        [
            "-loglevel",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=print_section=0",
            source,
        ],
    )
    times: list[float] = []
    for line in out.decode("utf-8", "replace").splitlines():
        # Some builds append a trailing `,` when a frame carries side data, and a
        # frame without a timestamp reports `N/A`: keep the first field, skip what
        # doesn't parse.
        field = line.split(",", 1)[0].strip()
        if not field:
            continue
        try:
            times.append(float(field))
        except ValueError:
            continue
    return times


async def probe_duration(ffprobe: str, source: Arg) -> float:
    """Total duration (seconds) of the source container. The last chunk runs from
    the final usable keyframe to here."""
    out = await _exec(
        ffprobe,
        [
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=print_section=0",
            "-i",
            source,
            "-hide_banner",
        ],
    )
    text = out.decode("utf-8", "replace").strip()
    try:
        return float(text)
    except ValueError as exc:
        raise TranscodeToolError(f"could not parse duration {text!r}") from exc
