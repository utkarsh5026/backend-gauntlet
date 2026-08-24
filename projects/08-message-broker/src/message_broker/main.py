"""Mini message broker (Kafka-lite) — entrypoint and wiring.

The plumbing (config, the on-disk broker layout, the FastAPI app, graceful
shutdown) is wired for you. The learning lives in the modules marked `TODO(Vx)`:
the segmented append-only log (V1, `log.py`), the sparse offset index (V2,
`index.py`), partitions + the partitioner (V3, `topic.py`), and consumer groups +
durable offset commits (V4, `group.py`). See SPEC.md.

There is no external dependency: the filesystem IS the broker. Scaffold state:
this starts and serves. `GET /healthz`, `GET /metrics`, `POST /topics` and
`GET /topics` all work; the first real produce or fetch raises a
`NotImplementedError` — that message is your worklist.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .broker import Broker
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .state import AppState

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent broker over a temp directory without touching the environment —
    which matters more here than usual, since two brokers sharing a `data_dir`
    would be two writers on one log.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Opens the on-disk layout: topics/<topic>/<partition>/ trees of segment
        # + index files (V1/V2) and groups/ for committed offsets (V4). Reloading
        # a topic is also where each partition's log recovers its own offsets.
        broker = Broker.open(cfg.data_dir, cfg.log_config, cfg.default_partitions)
        app.state.app_state = AppState(settings=cfg, broker=broker)

        logger.info(
            "broker opened",
            data_dir=str(cfg.data_dir),
            segment_bytes=cfg.segment_bytes,
            index_interval_bytes=cfg.index_interval_bytes,
            default_partitions=cfg.default_partitions,
            topics=[t.name for t in broker.list_topics()],
        )
        try:
            yield
        finally:
            # Runs after uvicorn has drained in-flight requests on SIGTERM, so
            # nothing is mid-append while these fsyncs happen. A clean stop must
            # leave no torn tail and lose no acknowledged write — that is the
            # "graceful shutdown" horizontal item.
            try:
                await broker.close()
            except NotImplementedError as exc:
                # V1/V4 are not built yet. Report it, but never let an unwritten
                # flush turn a clean shutdown into a crash.
                logger.warning("durable flush is still a todo", detail=str(exc))
            logger.info("shutdown complete")

    app = FastAPI(
        title="message-broker",
        summary="A Kafka-lite message broker built from the log up (project 08).",
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
        addr=f"0.0.0.0:{cfg.port}",
        data_dir=str(cfg.data_dir),
        hint="POST /topics then POST /topics/{topic}/records to produce",
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
