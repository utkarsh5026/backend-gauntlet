"""DynamoDB data plane — entrypoint and wiring.

The plumbing (config, the catalog, the FastAPI app, telemetry, graceful shutdown)
is wired for you. The learning lives in the modules marked `TODO(Vx)`: the item
model and key encoding (V1, `table.py`), secondary indexes (V2, `indexes.py`),
conditional writes and transactions (V3, `conditions.py`), provisioned throughput
(V4, `throughput.py`) and streams (V5, `streams.py`). See SPEC.md.

Scaffold state: this starts and serves. `GET /healthz`, `GET /tables`,
`POST /tables` and `GET /metrics` work; the first real data-plane operation raises
a `NotImplementedError` — that is your worklist.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Settings
from .errors import install_error_handlers
from .routes import public_router
from .state import AppState, Catalog

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent node without touching the environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        catalog = Catalog(cfg)
        app.state.app_state = AppState(settings=cfg, catalog=catalog)

        log.info(
            "node ready",
            data_dir=str(cfg.data_dir),
            max_item_bytes=cfg.max_item_bytes,
            partition_write_capacity=cfg.partition_write_capacity,
            stream_retention_hours=cfg.stream_retention_hours,
        )
        try:
            yield
        finally:
            # TODO(V1 / graceful shutdown): flush the write-ahead log here before
            # returning. Until the WAL exists there is nothing to flush — but the
            # ordering matters once it does: drain in-flight requests first, THEN
            # flush, or you can lose a write that was already acknowledged.
            log.info("shutdown complete")

    app = FastAPI(
        title="dynamodb-core",
        summary="A DynamoDB data plane built from the item up (project 23).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(public_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info(
        "starting",
        http_addr=f"0.0.0.0:{cfg.port}",
        hint="POST /tables to create one, then POST / with an X-Target header",
    )
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
        # access log is off because RequestIdMiddleware already emits one
        # structured line per request.
        loop="auto",
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
