"""LSM storage engine + redis-compatible server — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, opening (and recovering) the
`Engine`, the RESP TCP listener, an optional background compactor, the HTTP
sidecar, and graceful shutdown. The learning lives in the modules marked
`TODO(Vx)`:

* V1 `resp.py`        — the RESP wire codec, so real `redis-cli` connects
* V2 `wal.py`         — the write-ahead log, durability before the ack
* V3 `memtable.py`    — the sorted in-memory write buffer + tombstones
* V4 `sstable.py`     — the immutable sorted file + block index
* V5 `bloom.py`       — per-SSTable bloom filters
* V6 `compaction.py`  — background merge, bounding amplification
* V7 `block_cache.py` — a hand-built LRU over decoded SSTable blocks

`engine.py` ties them together; its read and write paths raise.

There is no external dependency: the filesystem (`DATA_DIR`) **is** the
database. No Postgres, no Redis. The `docker compose` service is only a
*reference* redis to run `redis-cli` / `redis-benchmark` against and to A/B your
semantics with.

Scaffold state: this starts and serves. `GET /healthz`, `/stats`, `/config` and
`/metrics` all answer, and the RESP port accepts connections — the first
*command* raises `NotImplementedError` naming the vertical it needs, which ends
that connection while the server keeps running. That message is your worklist.

## Two servers, one process

The RESP data plane is `asyncio.start_server` (see `server.py`); the sidecar is
FastAPI under uvicorn. Uvicorn runs in the foreground and owns the signal
handling, and the RESP server is started and stopped inside the lifespan. That
is the same shape the Rust version had, and it buys the thing that matters:
`uvicorn.run` installs the SIGTERM handler, so `docker stop` triggers the
lifespan's `finally`, which drains RESP connections and then flushes the WAL.
Wire it the other way around and shutdown becomes yours to reimplement.

Shutdown order in that `finally` is load-bearing and reads backwards from the
promise: nothing acknowledged may be lost, so the WAL is flushed **last**, after
every in-flight command has finished writing to it. Close the engine first and
you have fsynced a log that is missing the last few commands you then went on to
accept.

## What CPython costs you here, stated up front

The boss fight asks for 20000 SET/sec and 50000 GET/sec at p99 ≤ 10 ms. Those
numbers are not scaled down for Python, deliberately — where CPython cannot
reach one, *the gap is the finding*, and it belongs in
`docs/22-benchmarks.md` with its cause named: the GIL serializing the merge, the
per-command bytecode in the codec, an `os.fsync` on the event loop, allocation
churn in the parser, GC pauses under a large memtable. "Python is slow" is not a
finding. "The parser allocates three `bytes` objects per command and at 50k
ops/sec that is the top frame in the flamegraph" is.

`uvicorn[standard]` installs uvloop, and `loop="auto"` picks it — so the process
you ship runs a different event loop from the one pytest runs on. That is why
the SPEC's Definition of done asks you to boot the container: some bugs exist
under exactly one of the two.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .compaction import compaction_loop
from .config import Settings
from .engine import Engine
from .errors import install_error_handlers
from .routes import router
from .server import RespServer
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 15
"""How long uvicorn lets in-flight HTTP requests finish after SIGTERM.

The sidecar's requests are trivially fast, so this is really a budget for the
*lifespan* teardown that follows them: draining RESP connections and the final
WAL fsync. Generous, because the alternative to waiting is losing an
acknowledged write, and there is no amount of deploy speed worth that."""

COMPACTION_SHUTDOWN_BUDGET = 10.0
"""Seconds to wait for the compactor to stop after cancellation.

Longer than you would give most background tasks: a compaction may be mid-merge
holding a partly-written output file, and the safe thing is to let it notice the
cancellation at its next yield point rather than to abandon it. Whether your
compactor *has* frequent yield points is a design decision — see
`compaction.py`."""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI sidecar, with the engine and the RESP server on its
    lifespan.

    A factory rather than a module-level `app` so tests can stand up an
    independent engine over a temp directory without touching the environment.
    That matters more here than in most projects: two engines sharing one
    `data_dir` are two writers on one WAL, and the resulting corruption would
    not surface until a recovery, in a different test, looking like a flake.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Blocking, and correct: recovery runs before the listeners exist, so
        # there is nothing to starve. See engine.py on why that distinction is
        # the whole of the "no blocking on the loop" rule.
        engine = Engine.open(cfg)
        app.state.app_state = AppState(settings=cfg, engine=engine)

        resp_server = RespServer(engine, cfg)
        await resp_server.start(port=cfg.resp_port)
        app.state.resp_server = resp_server

        compactor: asyncio.Task[None] | None = None
        if cfg.run_compaction:
            # Held in a local for the whole lifespan: a bare `create_task`
            # result that nobody keeps can be garbage-collected mid-flight, and
            # here that means compaction silently stopping while /healthz still
            # answers `ok` — the write stall arriving with no warning at all.
            compactor = asyncio.create_task(
                compaction_loop(engine, cfg.compaction_interval),
                name="compaction",
            )
            logger.info("background compaction started", interval_ms=cfg.compaction_interval_ms)
        else:
            logger.info("background compaction disabled (RUN_COMPACTION=false)")

        if cfg.auth_required:
            logger.info("RESP auth enabled: clients must AUTH before any command")

        try:
            yield
        finally:
            # Order matters — see the module docstring. Stop compacting, stop
            # serving, and only then make the log durable.
            if compactor is not None:
                compactor.cancel()
                try:
                    async with asyncio.timeout(COMPACTION_SHUTDOWN_BUDGET):
                        await compactor
                except (TimeoutError, asyncio.CancelledError):
                    pass

            await resp_server.close()
            await engine.close()
            logger.info("shutdown complete")

    app = FastAPI(
        title="lsm-redis",
        summary="An LSM storage engine behind a redis-compatible RESP server (project 22).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    logger.info(
        "starting",
        resp_addr=f"0.0.0.0:{cfg.resp_port}",
        http_addr=f"0.0.0.0:{cfg.http_port}",
        data_dir=str(cfg.data_dir),
        hint=f"redis-cli -p {cfg.resp_port} ping",
    )
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.http_port,
        # "auto" picks uvloop, which uvicorn[standard] installs — and which is
        # *not* the loop pytest runs on. See the module docstring.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request;
        # uvicorn's own access log would just double the I/O.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
