"""Distributed job queue — entrypoint and wiring.

Everything the service needs is assembled once in the lifespan: the Postgres pool,
the queue handle, and — when `RUN_WORKERS=true` — the worker pool, the lease reaper
(V2) and the gauge sampler. Handlers reach it through `request.app.state.app_state`.

The shutdown half of the lifespan is where graceful shutdown actually lives. By the
time it runs, uvicorn has already stopped accepting connections and let in-flight
requests finish; what is left is to stop the background tasks. Setting the shutdown
event makes every worker stop *claiming new work* and return after the job in hand,
which is the ordering that matters: a worker killed mid-job leaves its row `running`
until the lease expires, and then someone runs it again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Final

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import lease, metrics, worker
from .config import Settings
from .db import create_pool
from .errors import install_error_handlers
from .queue import Queue
from .routes import BodyLimitMiddleware, protected_router, public_router
from .state import AppState
from .worker import WorkerConfig

log = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

SHUTDOWN_BUDGET: Final = 15.0
"""Seconds to wait for workers, reaper and sampler to stop before giving up.

Comfortably under a typical orchestrator's SIGTERM->SIGKILL grace period (30s on
Kubernetes), so the process exits on its own terms rather than being killed with a
worker still mid-job."""


def worker_config(cfg: Settings) -> WorkerConfig:
    """Translate settings into the per-worker tuning."""
    return WorkerConfig(
        queue_name=cfg.queue,
        poll_interval=cfg.poll_interval,
        visibility_timeout=cfg.visibility_timeout,
        claim_batch=cfg.claim_batch,
        log_dir=cfg.job_log_dir,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent instance (its own pool, its own queue name) without touching the
    process environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        pool = await create_pool(
            cfg.database_url, min_size=cfg.db_pool_min, max_size=cfg.db_pool_max
        )
        log.info("connected to postgres", pool_max=cfg.db_pool_max)

        queue = Queue(pool, cfg.default_max_attempts)
        shutdown = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []

        if cfg.run_workers:
            wcfg = worker_config(cfg)
            # Tasks are held in a list for the whole lifespan: a bare `create_task`
            # result that nobody keeps can be garbage-collected mid-flight, taking
            # the worker with it.
            tasks.extend(
                _spawn(worker.run(f"worker-{n}", queue, wcfg, shutdown), f"worker-{n}")
                for n in range(cfg.worker_concurrency)
            )
            tasks.append(
                _spawn(
                    lease.reap_loop(pool, float(cfg.reaper_interval_secs), shutdown),
                    "lease-reaper",
                )
            )
            tasks.append(
                _spawn(
                    metrics.gauge_loop(pool, cfg.queue, float(cfg.gauge_interval_secs), shutdown),
                    "gauge-sampler",
                )
            )
            log.info(
                "worker pool started",
                concurrency=cfg.worker_concurrency,
                queue=cfg.queue,
                pool_max=cfg.db_pool_max,
            )
        else:
            log.info("workers disabled (RUN_WORKERS=false): enqueue API only")

        if cfg.token is None:
            log.warning(
                "ENQUEUE_TOKEN unset — POST /jobs and requeue are UNAUTHENTICATED "
                "(dev only; the exec/shell job kinds make this remote code execution)"
            )

        app.state.app_state = AppState(settings=cfg, queue=queue)
        log.info("ready", queue=cfg.queue, workers=cfg.worker_concurrency if cfg.run_workers else 0)

        try:
            yield
        finally:
            shutdown.set()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_BUDGET)
                if pending:
                    log.warning(
                        "background tasks exceeded shutdown budget; cancelling",
                        pending=len(pending),
                        budget_secs=SHUTDOWN_BUDGET,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        log.error("background task failed", task=task.get_name(), error=str(exc))
            await pool.close()
            log.info("shutdown complete")

    app = FastAPI(
        title="job-queue",
        summary="A SKIP LOCKED claim engine, leases, backoff + DLQ, and LISTEN/NOTIFY scheduling.",
        lifespan=lifespan,
    )

    # Order matters: the last one added is the outermost. The body limit goes on
    # first so it sits *inside* RequestIdMiddleware — a 413 is still a request, and
    # it should be logged and carry a request id like every other response.
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)

    app.include_router(public_router)
    app.include_router(protected_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def _spawn(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    return asyncio.create_task(coro, name=name)


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info("starting", addr=f"0.0.0.0:{cfg.port}", hint="POST /jobs to enqueue")
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
        # access log is off because RequestIdMiddleware already emits one
        # structured line per request — two would just double the I/O.
        loop="auto",
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
