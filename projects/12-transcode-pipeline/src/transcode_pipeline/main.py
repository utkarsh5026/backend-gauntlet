"""Distributed transcoding pipeline — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the Postgres pool, the
coordinator's scheduler loop, the worker pool, the FastAPI control-plane API, and
graceful shutdown. The learning lives in the modules marked `TODO(Vx)`:

* V1 `chunk.py`  — keyframe-aligned chunking
* V2 `dag.py`    — the job DAG + readiness rule, with its durable twin in `store.py`
* V3 `worker.py` — idempotent parallel transcodes, with the lease in `store.py`
* V4 `stitch.py` — the seamless stitch/remux

Scaffold state: this starts and serves once `make up` has Postgres running.
`/healthz` and `/metrics` answer; `POST /jobs` and `GET /jobs/{id}` answer `501`
naming the V2 todo. Setting `RUN_WORKERS=true` starts the scheduler and the
workers, and each one stops on its first store call, logging the todo that stopped
it — that is your worklist. `ffmpeg` / `ffprobe` must be on `PATH` (or set
`FFMPEG_BIN` / `FFPROBE_BIN`) once real transcodes run.

## Shutdown order

`uvicorn.run` installs the SIGTERM handler. On SIGTERM uvicorn stops accepting
connections and lets in-flight requests finish; *then* the lifespan's `finally`
runs, which:

1. sets `state.shutdown` — every worker stops **claiming** (it checks before each
   claim) and the scheduler stops ticking;
2. waits up to `WORKER_DRAIN_SECONDS` for in-flight tasks to finish and settle;
3. cancels whatever is still running. A cancelled encode kills its ffmpeg child
   (`ffmpeg.py`), and the task it held stays `RUNNING` until its lease expires and
   the reaper returns it to `READY` — in another process, or in this one's next
   start.

Step 3 is not a failure mode to design around; it is V3's crash safety doing its
job. Graceful shutdown is an optimisation on top of that, never a substitute.

## What CPython costs you here, stated up front

The Straggler asks for ≥ 6× speedup at 8 workers, faster than realtime, and a
recovery within 1.5× of the un-killed wall-clock. Those numbers are not scaled down
for Python. This project is unusually kind to CPython — the CPU is spent inside
ffmpeg, not the interpreter — so the orchestration overhead *should* disappear into
the noise. If it doesn't (claim round-trips, a blocking call on the loop, decoding a
3,600-task expansion), that gap and its cause are the finding, recorded in
`docs/12-benchmarks.md`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import asyncpg
import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import dag, db
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .state import AppState, task_failure
from .store import JobStore
from .workdir import WorkDir
from .worker import Worker

logger = structlog.get_logger(__name__)

__all__ = ["build_state", "create_app", "main", "start_background", "stop_background"]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn lets in-flight HTTP requests finish after SIGTERM. The API's
requests are short database round-trips, so in practice this phase is instant."""

WORKER_DRAIN_SECONDS = 15.0
"""How long shutdown waits for in-flight tasks to settle before cancelling them.

Chosen so both budgets together fit k8s' default 30 s grace period, after which the
kubelet sends SIGKILL and nothing in the `finally` runs. (`docker stop` defaults to
10 s — pass `-t 30` to watch a full drain.) A real encode often takes longer than
this, which is fine: the lease is what recovers a cancelled one."""

CANCEL_SECONDS = 3.0
"""How long to wait for a cancelled task to actually stop (and reap its ffmpeg)."""


def build_state(settings: Settings, *, pool: asyncpg.Pool[asyncpg.Record]) -> AppState:
    """Assemble the pipeline over an already-constructed pool. No I/O.

    Split out from the lifespan so tests can build the whole app over a pool that
    never connects — asyncpg's `create_pool()` returns an unconnected pool until it
    is awaited. The scaffold suite runs in CI that way, with no Postgres at all.
    """
    return AppState(
        settings=settings,
        pool=pool,
        store=JobStore(pool),
        workdir=WorkDir(settings.work_dir),
    )


def _log_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead background task the moment it dies.

    A task that raises does so *silently*: nothing is printed until someone awaits
    it or the garbage collector complains. For a worker, that silence looks exactly
    like an idle pool — jobs just stop draining. This callback is the fix, and the
    habit to keep for every long-lived task.
    """
    exc = task_failure(task)
    if exc is not None:
        logger.error(
            "background task died",
            task=task.get_name(),
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


def start_background(state: AppState) -> None:
    """Start the scheduler and `WORKER_CONCURRENCY` workers as tasks on this loop."""
    settings = state.settings
    tasks = [
        asyncio.create_task(
            dag.schedule_loop(state.store, settings.scheduler_interval_secs, state.shutdown),
            name="scheduler",
        )
    ]
    for n in range(settings.worker_concurrency):
        # The pid keeps ids distinct when the pool runs as several processes.
        worker = Worker(f"worker-{os.getpid()}-{n}", state.store, state.workdir, settings)
        tasks.append(asyncio.create_task(worker.run(state.shutdown), name=worker.worker_id))
    for task in tasks:
        task.add_done_callback(_log_task_exit)
        state.background.append(task)
    logger.info("scheduler + worker pool started", concurrency=settings.worker_concurrency)


async def stop_background(state: AppState, *, drain_secs: float = WORKER_DRAIN_SECONDS) -> None:
    """Stop claiming, let in-flight tasks settle within `drain_secs`, cancel the rest."""
    if not state.background:
        return
    state.shutdown.set()
    _, pending = await asyncio.wait(state.background, timeout=drain_secs)
    if not pending:
        logger.info("background tasks drained")
        return
    logger.warning(
        "drain budget exhausted: cancelling in-flight tasks, their leases will return them",
        cancelled=len(pending),
    )
    for task in pending:
        task.cancel()
    await asyncio.wait(pending, timeout=CANCEL_SECONDS)


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the ASGI app.

    With no `state`, the lifespan opens the pool itself (production). With a
    `state`, it installs that one and opens and closes nothing — the caller owns the
    pool. That is how the tests run the real app over a pool that never connects.
    """
    config = state.settings if state is not None else (settings or Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            if state is None:
                # Never log the URL: DATABASE_URL carries a password.
                pool = await db.create_pool(
                    config.database_url, min_size=1, max_size=config.db_max_connections
                )
                stack.push_async_callback(pool.close)
                logger.info("connected to postgres")
                app_state = build_state(config, pool=pool)
            else:
                app_state = state
            app.state.app_state = app_state

            if config.run_workers:
                start_background(app_state)
            else:
                logger.info("workers disabled (RUN_WORKERS=false): control-plane API only")
            try:
                yield
            finally:
                # Before the pool closes (the exit stack unwinds after this): a
                # draining worker still needs a connection to settle its task.
                await stop_background(app_state)
                logger.info("shutdown complete")

    app = FastAPI(
        title="transcode-pipeline",
        summary="Distributed transcoding: keyframe chunking, a durable DAG, parallel workers.",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    config = Settings()
    common_telemetry.init(config.log_level)
    logger.info(
        "starting",
        http_addr=f"0.0.0.0:{config.port}",
        run_workers=config.run_workers,
        worker_concurrency=config.worker_concurrency,
    )
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.port,
        # "auto" picks uvloop, which uvicorn[standard] installs — and which is *not*
        # the loop pytest runs on. That is why the Python checklist asks you to boot
        # the container.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
