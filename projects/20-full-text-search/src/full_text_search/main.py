"""Full-text search engine (Elasticsearch-lite) — entrypoint and wiring.

The plumbing (config, the sharded on-disk layout, the FastAPI app, `/metrics`, an
optional background refresher, graceful shutdown) is wired for you. The learning
lives in the modules marked `TODO(Vx)`: the analyzer (V1, `analyzer.py`), the
on-disk inverted-index segments (V2, `segment.py`), BM25 ranking (V3, `bm25.py`),
segment merging and deletes (V4, `merge.py`) and scatter-gather across shards
(V5, `shard.py`). The query cache (`cache.py`) is the caching horizontal. See
SPEC.md.

There is no external dependency: the filesystem IS the index — no Postgres, no
Redis, no Elasticsearch. Scaffold state: this starts and serves. `GET /healthz`,
`GET /_stats`, `GET /metrics`, `POST /_refresh` and `POST /_forcemerge` all work;
the first real index, search or delete raises a `NotImplementedError`, and that
message is your worklist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .analyzer import Analyzer, AnalyzerConfig
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .shard import ShardedIndex
from .state import AppState

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so a test can construct an
    independent engine — its own index directory, its own shard count — without
    touching the environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # One analyzer, shared by the index path and the query path (V1). Both
        # run identical analysis, which is what makes a query match a document.
        analyzer = Analyzer(AnalyzerConfig())
        engine = ShardedIndex(cfg.engine, analyzer)
        app.state.app_state = AppState(settings=cfg, engine=engine)

        log.info(
            "index opened",
            index_dir=str(cfg.index_dir),
            shards=cfg.shard_count,
            merge_factor=cfg.merge_factor,
            query_cache_cap=cfg.query_cache_cap,
        )

        # Optional background refresher: flush each shard's buffer into a segment
        # on a fixed cadence (near-real-time search). Off by default
        # (REFRESH_INTERVAL_MS=0) so the bare scaffold serves without a task
        # raising on the not-yet-built V2 flush — call POST /_refresh by hand
        # instead. Turn it on once V2 works.
        refresher: asyncio.Task[None] | None = None
        if cfg.refresh_interval_ms > 0:
            # Held in a variable, not just created: asyncio keeps only a weak
            # reference to a running task, so a bare `create_task(...)` can be
            # garbage-collected mid-flight and simply stop refreshing.
            refresher = asyncio.create_task(
                refresh_loop(engine, cfg.refresh_interval_ms / 1000), name="refresher"
            )
            log.info("background refresher started", interval_ms=cfg.refresh_interval_ms)
        else:
            log.info("background refresher disabled — refresh via POST /_refresh")

        try:
            yield
        finally:
            # Everything past this line runs on SIGTERM, after uvicorn has
            # stopped accepting connections and drained in-flight requests.
            if refresher is not None:
                refresher.cancel()
                with suppress(asyncio.CancelledError):
                    await refresher

            # TODO(graceful shutdown): flush each shard's buffer here (a final
            # `await engine.refresh_all()`) so buffered-but-unrefreshed documents
            # are not silently lost — or decide the opposite and state it. The
            # SPEC accepts either, but only if it is written down: "a document is
            # durable once refreshed, and un-refreshed documents are lost on
            # shutdown" is a contract; losing them by accident is a bug. Note the
            # refresher is cancelled first, so nothing races the final flush.

            engine.close()
            log.info("shutdown complete")

    app = FastAPI(
        title="full-text-search",
        summary="An Elasticsearch-lite built from the inverted index up (project 20).",
        lifespan=lifespan,
    )
    # Outermost, so every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


async def refresh_loop(engine: ShardedIndex, interval_seconds: float) -> None:
    """Periodically refresh every shard so buffered documents become searchable.

    Runs until cancelled by the lifespan teardown. The broad `except` is
    deliberate: a failed refresh must be logged and retried on the next tick, not
    kill the loop and leave the index quietly frozen with no error anywhere.
    `CancelledError` is re-raised because it is shutdown, not a failure — and
    since 3.8 it inherits from `BaseException`, so `except Exception` already
    lets it through.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await engine.refresh_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("background refresh failed", error=str(exc))


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info(
        "starting",
        addr=f"0.0.0.0:{cfg.port}",
        hint="POST /documents to index, POST /_refresh, then GET /search?q=…",
    )
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
