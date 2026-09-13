"""V3 — Parallel transcode workers: run the DAG's tasks, idempotently.

A worker is a loop: **claim** a `READY` task (leased, `SKIP LOCKED`), **run** it,
**settle** it (`complete` on success, `fail` → retry or dead-letter on error), then
go again. Run several and the DAG's fan-out becomes real parallelism — dozens of
chunk transcodes in flight at once, one per worker.

The loop below is wired. The learning is what a worker *does*, and how safely:

* **`Split`** — probe the source, plan chunks (V1), expand the DAG (V2). Wired down
  to those two calls.
* **`Transcode`** — the crux of V3: run ffmpeg to transcode *exactly one chunk* at
  one rendition, **idempotently**. Because a lease can expire and a task re-run
  (at-least-once), a re-run must reproduce the same chunk bytes and commit them
  atomically (write temp, then rename), so a duplicate run is harmless and a
  half-written file is never mistaken for a finished one.
* **`Stitch`** — hands off to V4.

## One process, N workers, one thread

A worker here is an asyncio task — not a thread, not a process — and that is the
right shape for this job. A worker spends its life *waiting*: on Postgres, and on an
ffmpeg child doing the actual encode. Waiting is what asyncio is for, and the
encoding CPU burns inside ffmpeg, outside the interpreter, where the GIL never sees
it. `WORKER_CONCURRENCY` is therefore a cap on concurrent *ffmpeg processes*, sized
to the box's cores, and that cap is the backpressure: a flood of `READY` chunks
becomes a longer queue, not 300 encoders thrashing 16 cores. The way to defeat it is
to start an encode without awaiting it — a fire-and-forget `create_task` per claim.

To `kill -9` *one* worker — V3's recovery criterion, and the boss fight — run the
pool as several processes rather than one process with a large
`WORKER_CONCURRENCY`.

## Why an unbuilt vertical ends the worker instead of failing the task

`NotImplementedError` is re-raised, never settled with `fail`. Settling it would
dead-letter every task in the database on the scaffold's first run, and a worker
that logged and carried on would bury the todo under a log line per poll. Letting
it propagate ends this worker, and `main` logs the todo once, loudly. Any other
exception is a genuinely failed attempt and goes through `fail`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from . import dag, ffmpeg
from .chunk import plan_chunks
from .config import Settings
from .models import Split, Stitch, Task, Transcode
from .state import wait_for_shutdown
from .stitch import stitch
from .store import JobStore
from .workdir import WorkDir

__all__ = ["Worker"]

logger = structlog.get_logger(__name__)


class Worker:
    """One worker: an id plus shared handles to the store, the artifact layout, and
    settings."""

    def __init__(
        self, worker_id: str, store: JobStore, workdir: WorkDir, settings: Settings
    ) -> None:
        self.worker_id = worker_id
        self._store = store
        self._workdir = workdir
        self._settings = settings
        self._log: Any = logger.bind(worker=worker_id)

    async def run(self, shutdown: asyncio.Event) -> None:
        """Drain the DAG until shutdown: claim → run → settle → repeat.

        Shutdown is checked *before* each claim and never interrupts `process`: a
        worker told to stop finishes the task it holds, settles it, then exits. If
        the drain budget in `main` runs out first, this task is cancelled, and the
        lease — not this loop — is what recovers the chunk.
        """
        self._log.info("worker started")
        while not shutdown.is_set():
            # V3: claim one READY task, leased to this worker.
            try:
                task = await self._store.claim_ready(self.worker_id, self._settings.lease_secs)
            except NotImplementedError:
                raise
            except Exception as exc:
                self._log.error("claim failed", error=str(exc), kind=type(exc).__name__)
                task = None

            if task is None:
                # Nothing ready (or the claim failed): wait, but stay responsive to
                # shutdown.
                await wait_for_shutdown(shutdown, self._settings.poll_interval_secs)
                continue

            await self.process(task)
        self._log.info("worker stopped")

    async def process(self, task: Task) -> None:
        """Run a single claimed task and settle it."""
        log = self._log.bind(
            job_id=str(task.job_id), task_id=str(task.id), op=task.kind.op, attempt=task.attempts
        )
        log.debug("running task")

        try:
            await self.execute(task)
        except NotImplementedError:
            raise
        except Exception as exc:
            # V3: retry-with-backoff or dead-letter, based on attempts.
            log.warning("task failed", error=str(exc), kind=type(exc).__name__)
            try:
                await self._store.fail(task.id, str(exc), self._settings.max_attempts)
            except NotImplementedError:
                raise
            except Exception as settle_exc:
                log.error("settling failed task failed", error=str(settle_exc))
            return

        try:
            await self._store.complete(task.id)
        except NotImplementedError:
            raise
        except Exception as exc:
            # The artifact is committed but the ack is lost: the lease expires, the
            # task re-runs, and V3's idempotency makes that re-run harmless.
            log.error("complete failed", error=str(exc))

    async def execute(self, task: Task) -> None:
        """Dispatch a task to its handler by kind.

        `Split` is wired down to the two vertical calls (V1 `plan_chunks`, V2
        `expand`); `Transcode` and `Stitch` hand off to the unbuilt parts.
        """
        match task.kind:
            case Split():
                await self._split(task)
            case Transcode(chunk=index, rendition=rendition):
                await self.transcode_chunk(task, index, rendition)
            case Stitch(rendition=rendition):
                await stitch(
                    self._settings.ffmpeg_bin,
                    self._workdir.chunk_dir(task.job_id, rendition),
                    self._workdir.rendition_output(task.job_id, rendition),
                )

    async def _split(self, task: Task) -> None:
        """Probe (plumbing) → plan chunks (V1) → expand the DAG (V2)."""
        ctx = await self._store.job_context(task.job_id)
        # The traversal guard resolves through the filesystem: keep it off the loop.
        source = await asyncio.to_thread(self._workdir.resolve_source, ctx.source)

        keyframes = await ffmpeg.probe_keyframes(self._settings.ffprobe_bin, source)
        duration = await ffmpeg.probe_duration(self._settings.ffprobe_bin, source)

        chunks = plan_chunks(keyframes, duration, self._settings.target_chunk_secs)
        self._log.info(
            "planned chunks",
            job_id=str(task.job_id),
            chunks=len(chunks),
            renditions=len(ctx.ladder),
        )

        tasks = dag.expand(task.job_id, task.id, chunks, ctx.ladder)
        await self._store.add_tasks(tasks)

    async def transcode_chunk(self, task: Task, chunk: int, rendition: str) -> None:
        """Transcode one chunk at one rendition — the idempotent unit of parallel work.

        TODO(V3): build and run the ffmpeg command that transcodes just this chunk.
          - Cut the source to this chunk's `[start, end)` and encode it at
            `rendition` (scale to its height, its bitrates) with
            `ffmpeg.run(self._settings.ffmpeg_bin, args)` — an argv list.
          - The task carries only the chunk *index* and the rendition *name*. The
            bitrates come from the job's ladder (`store.job_context`); the span comes
            from V1's plan. How the span gets here — re-planning from a fresh probe,
            which V1's purity makes deterministic, or widening `Transcode` to carry
            it — is a design decision for `docs/12-design.md`.
          - **Deterministic**: fixed encoder settings, no wall-clock or random
            metadata in the container, so a re-run yields byte-identical output
            (`test_transcode_is_deterministic`).
          - **Atomic**: encode to a temp path beside the final one, then `os.replace`
            it into `self._workdir.chunk_dir(task.job_id, rendition) / f"{chunk}.mp4"`
            — a killed attempt leaves no half-file at the final path, so a file *at*
            the final path is a truthful "already done" (the memoization checklist
            item).
        """
        raise NotImplementedError(
            "V3: deterministically transcode this one chunk, commit atomically (temp→rename)"
        )
