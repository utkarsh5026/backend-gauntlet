"""The dispatch layer: turn a stored job's `(kind, payload)` into a typed handler.

This lives *outside* the queue core on purpose. :mod:`job_queue.queue` and
:mod:`job_queue.job` stay generic — to them a job is an opaque `kind` string plus a
JSON `payload`, and the claim/lease/retry/schedule mechanics (V1–V4) never care
what a job *does*. The closed catalogue of things a worker knows how to run lives
here, so adding a job type is a change to the app layer, not to the queue's row
type. The queue never trusts `kind` to be anything but a routing key.

The catalogue is a **pydantic discriminated union**: `kind` is the discriminator,
`payload` is the variant's arguments, and decoding is one `TypeAdapter` call that
either yields a fully-typed handler or fails. An unknown `kind` or a payload of the
wrong shape is a *decode* failure, which becomes a job failure — never a crash, and
never something that runs.

Security — the :class:`Exec` / :class:`Shell` kinds are RCE **by design**
------------------------------------------------------------------------
A job that runs a program (or a `sh -c` line) whose contents came from a
`POST /jobs` body turns the enqueue endpoint into arbitrary code execution on every
worker. That is legitimate for a CI-runner / task-runner, but it means:

* the enqueue API **must** be authenticated — an open `POST /jobs` is an open root
  shell, not just "make my workers busy";
* payloads must never be logged blindly (an arg or env var may be a secret) — which
  is why command output goes to a per-attempt *file* and never into the structured
  log stream;
* in anything real you'd want an allow-list of programs and a sandboxed,
  unprivileged, resource-capped child. A raw `sh -c` is the maximal surface. Here
  it is deliberately plain so the *queue* behaviour is what's on show.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import signal
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import httpx
import structlog
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .job import Job

__all__ = [
    "DEFAULT_EXEC_TIMEOUT",
    "KILL_GRACE",
    "MAX_LOG_LINES",
    "Echo",
    "Exec",
    "Fail",
    "FlakyThenOk",
    "JobFailed",
    "JobKind",
    "Noop",
    "Shell",
    "Sleep",
    "Webhook",
    "decode",
    "dispatch",
    "job_log_path",
]

log = structlog.get_logger(__name__)

DEFAULT_EXEC_TIMEOUT: Final = 20.0
"""Wall-clock cap applied to an exec/shell job when its payload omits `timeout_secs`.

A command that runs longer than the worker's visibility timeout is still `running`
when its lease expires, so the reaper returns the job to `ready` and a second worker
starts a **concurrent copy**. Keep this comfortably under
`VISIBILITY_TIMEOUT_SECS`."""

MAX_STDERR: Final = 2000
"""Max bytes of the stderr tail folded into the failure / `last_error` message."""

MAX_LOG_LINES: Final = 1000
"""Cap on how many output lines a single job may write to its log file.

Past this we stop *writing* but keep draining the pipes — a child whose output
buffer fills blocks forever, so "stop reading" is not an option a timeout can save
you from. A runaway job can't flood the disk, and it also can't wedge the worker."""

STDERR_TAIL_LINES: Final = 20
"""How many trailing stderr lines to keep for the failure message."""

KILL_GRACE: Final = 5.0
"""Seconds to wait for a killed process group to be reaped before giving up."""


class JobFailed(Exception):
    """A job ran and did not succeed.

    Distinct from an unexpected exception only in intent: both are nacked with
    their message as `last_error`, but this one says the failure is the job's own
    (a poison message, a non-2xx webhook, a non-zero exit) rather than a bug in the
    worker.
    """


def job_log_path(base: Path, job: Job) -> Path:
    """Path for this attempt: `{base}/{job.id}/{job.attempts}.log`.

    Keyed by attempt as well as id so a retry doesn't overwrite the evidence from
    the attempt that failed — which is usually the one you want to read.
    """
    return base / str(job.id) / f"{job.attempts}.log"


# --------------------------------------------------------------------------- #
# Process execution shared by Exec and Shell
# --------------------------------------------------------------------------- #


async def _pump(
    stream: asyncio.StreamReader,
    tag: str,
    sink: list[str],
    tail: collections.deque[str] | None,
    written: list[int],
    out: Any,
) -> None:
    """Drain one pipe to EOF, writing tagged lines until the cap.

    Keeps reading past the cap on purpose (see :data:`MAX_LOG_LINES`): the goal is
    to bound the *file*, not to stop consuming, because a child blocked on a full
    pipe never exits and would outlive its lease.
    """
    _ = sink
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip("\n")
        if written[0] < MAX_LOG_LINES:
            out.write(f"[{tag}] {line}\n")
        elif written[0] == MAX_LOG_LINES:
            out.write(f"… truncated at {MAX_LOG_LINES} lines\n")
        written[0] += 1
        if tail is not None:
            tail.append(line)


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child **and everything it spawned**, then reap it.

    Killing only the direct child is not enough, and the failure is subtle. `sh -c
    "sleep 30"` may fork rather than exec, so killing `sh` leaves `sleep` running —
    and that orphan inherited the stdout/stderr pipes. asyncio treats a subprocess
    as finished only once it has exited *and* every pipe has hit EOF, so
    `proc.wait()` would then block until the orphan finished anyway: a job with a
    0.5s timeout would take the command's full 30 seconds to "time out".

    That is not cosmetic. The whole point of the timeout is that a hung command must
    not outlive its lease (V2) — when it does, the reaper requeues the job and a
    *second copy* starts while the first is still running.

    Spawning with `start_new_session=True` puts the child in its own process group,
    so one `killpg` takes the whole tree down, the pipes close, and the wait returns.
    The wait is still bounded: a process wedged in uninterruptible I/O cannot be
    killed at all, and blocking on it forever would hand the worker the exact
    problem the timeout exists to prevent.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
        proc.kill()
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        async with asyncio.timeout(KILL_GRACE):
            await proc.wait()


async def _run_process(proc: asyncio.subprocess.Process, timeout: float, log_path: Path) -> None:
    """Stream both pipes into `log_path`, enforce `timeout`, and map the outcome.

    Exit `0` returns; a non-zero exit or a timeout raises :class:`JobFailed` with a
    reason that becomes `last_error`.

    Two choices tie back to the verticals:

    * the timeout **kills** a hung command (and its whole process group — see
      :func:`_kill_process_group`) rather than letting it outlive its lease (V2)
      and get a concurrent second copy started by the reaper;
    * a non-zero exit is a failure, so it flows into backoff + DLQ (V3) — but
      retrying is only safe if the command is **idempotent**, since at-least-once
      may run it more than once.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
    written = [0]

    if proc.stdout is None or proc.stderr is None:  # pragma: no cover - both are PIPEd
        raise JobFailed("child process was created without pipes")

    with log_path.open("w", encoding="utf-8") as out:
        pumps = asyncio.gather(
            _pump(proc.stdout, "out", [], None, written, out),
            _pump(proc.stderr, "err", [], tail, written, out),
        )
        try:
            async with asyncio.timeout(timeout):
                await pumps
                await proc.wait()
        except TimeoutError:
            pumps.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pumps
            await _kill_process_group(proc)
            raise JobFailed(f"timed out after {timeout:g}s") from None

    log.info("job output captured", log=str(log_path), lines=written[0])

    if proc.returncode == 0:
        return

    message = "\n".join(tail).strip()
    if len(message) > MAX_STDERR:
        message = message[:MAX_STDERR] + "…(truncated)"
    code = proc.returncode if proc.returncode is not None and proc.returncode >= 0 else "signal"
    raise JobFailed(f"exit {code}: {message}")


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


class _Handler(BaseModel):
    """Base for every job kind. Subclasses implement :meth:`run`."""

    async def run(self, job: Job, log_dir: Path) -> None:
        """Do the work. Return to ack; raise to nack."""
        raise NotImplementedError  # pragma: no cover - abstract


class Noop(_Handler):
    """Succeed immediately; useful for enqueue/claim plumbing tests."""

    kind: Literal["noop"]
    payload: Any = None

    async def run(self, job: Job, log_dir: Path) -> None:
        return None


class _SleepArgs(BaseModel):
    ms: int = Field(ge=0)


class Sleep(_Handler):
    """Sleep for `ms` milliseconds then succeed."""

    kind: Literal["sleep"]
    payload: _SleepArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        await asyncio.sleep(self.payload.ms / 1000.0)


class _EchoArgs(BaseModel):
    msg: str


class Echo(_Handler):
    """Log `msg` then succeed."""

    kind: Literal["echo"]
    payload: _EchoArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        log.info("echo", msg=self.payload.msg, job_id=job.id)


class Fail(_Handler):
    """Always fail — a poison message that exercises retry / DLQ (V3)."""

    kind: Literal["fail"]
    payload: Any = None

    async def run(self, job: Job, log_dir: Path) -> None:
        raise JobFailed("poison")


class _FlakyArgs(BaseModel):
    fail_n: int


class FlakyThenOk(_Handler):
    """Fail while `job.attempts <= fail_n`, then succeed (flaky-downstream tests)."""

    kind: Literal["flaky_then_ok"]
    payload: _FlakyArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        if job.attempts <= self.payload.fail_n:
            raise JobFailed("poison")


class _WebhookArgs(BaseModel):
    url: str


class Webhook(_Handler):
    """HTTP POST to `url`; success iff the response status is 2xx."""

    kind: Literal["webhook"]
    payload: _WebhookArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        # A client per job, closed on the way out. A shared client would pool
        # connections better, but it would also outlive the worker's shutdown and
        # keep sockets open past the drain — not worth it for a job that makes one
        # request.
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.payload.url)
        except httpx.HTTPError as exc:
            raise JobFailed(f"webhook request failed: {exc}") from exc
        if not response.is_success:
            raise JobFailed(f"webhook failed: status {response.status_code}")


class _ExecArgs(BaseModel):
    program: str
    args: list[str] = Field(default_factory=list)
    timeout_secs: float | None = None


class Exec(_Handler):
    """Spawn `program` with `args`, capturing output to a per-attempt log file.

    See the module-level security note — this is remote code execution by design.
    """

    kind: Literal["exec"]
    payload: _ExecArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            self.payload.program,
            *self.payload.args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so a timeout can kill the whole tree (see
            # _kill_process_group), not just the shell that forked it.
            start_new_session=True,
        )
        timeout = self.payload.timeout_secs or DEFAULT_EXEC_TIMEOUT
        await _run_process(proc, timeout, job_log_path(log_dir, job))


class _ShellArgs(BaseModel):
    script: str
    timeout_secs: float | None = None


class Shell(_Handler):
    """Run `script` via `sh -c`, same capture / timeout behaviour as :class:`Exec`.

    See the module-level security note — this is remote code execution by design.
    """

    kind: Literal["shell"]
    payload: _ShellArgs

    async def run(self, job: Job, log_dir: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            self.payload.script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so a timeout can kill the whole tree (see
            # _kill_process_group), not just the shell that forked it.
            start_new_session=True,
        )
        timeout = self.payload.timeout_secs or DEFAULT_EXEC_TIMEOUT
        await _run_process(proc, timeout, job_log_path(log_dir, job))


JobKind = Annotated[
    Noop | Sleep | Echo | Fail | FlakyThenOk | Webhook | Exec | Shell,
    Field(discriminator="kind"),
]
"""The closed catalogue of jobs a worker knows how to run.

To teach the workers a new job type: add a `_Handler` subclass with its `kind`
literal and its payload model, then add it to this union. Nothing in the queue core
changes.
"""

_ADAPTER: Final = TypeAdapter[JobKind](JobKind)


def decode(job: Job) -> JobKind:
    """Rebuild the typed handler from the two stored columns.

    Raises :class:`JobFailed` when `kind` is unknown or `payload` doesn't match the
    variant's shape. That is deliberate: an unrecognised `kind` is a bad enqueue,
    not something to run, and routing it through the normal failure path means it
    gets recorded in `last_error` and dead-lettered like any other bad job instead
    of taking the worker down.
    """
    try:
        return _ADAPTER.validate_python({"kind": job.kind, "payload": job.payload})
    except ValidationError as exc:
        raise JobFailed(f"unroutable job: {exc.error_count()} validation error(s)") from exc


async def dispatch(job: Job, log_dir: Path) -> None:
    """Run one job to completion.

    Returning acks the job (→ `done`); raising drives the retry/DLQ path with the
    exception message recorded as `last_error`. Called from the worker's
    `process_one`.
    """
    await decode(job).run(job, log_dir)
