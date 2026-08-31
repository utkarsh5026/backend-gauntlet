"""URL shortener - entrypoint and wiring.

Everything the service needs is assembled once in the lifespan: the Postgres
pool, the Redis client, the id generator, the rate limiter, and the background
click ingestor. Handlers reach it through `request.app.state.app_state`.

The shutdown half of the lifespan is where the "graceful shutdown" checklist item
actually lives. By the time it runs, uvicorn has already stopped accepting
connections and let in-flight requests finish; what is left is to drain the click
buffer within a budget and close the two connection pools. On SIGTERM that whole
sequence happens before the process exits, so a clean stop loses no clicks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from .cache import Cache, create_redis
from .config import Settings
from .db import create_pool
from .errors import install_error_handlers
from .id_gen import IdGenerator
from .ingest import ClickIngestor
from .ratelimit import RateLimiter
from .routes import dashboard_dist, protected_router, public_router, redirect_router
from .shutdown import drain_ingestor
from .state import AppState

log = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent instance (its own pool, its own cache prefix) without touching
    the process environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        pool = await create_pool(
            cfg.database_url, min_size=cfg.db_pool_min, max_size=cfg.db_pool_max
        )
        log.info("connected to postgres", pool_max=cfg.db_pool_max)

        redis = create_redis(cfg.redis_url)
        log.info("redis client ready", url=_redact(cfg.redis_url))

        ingestor = ClickIngestor(pool)
        # Held in a local for the whole lifespan: a bare `create_task` result that
        # nobody keeps can be garbage-collected mid-flight, taking the ingestion
        # loop with it.
        ingest_task = asyncio.create_task(ingestor.run(), name="click-ingestor")

        app.state.app_state = AppState(
            settings=cfg,
            pool=pool,
            cache=Cache(redis),
            ids=IdGenerator(cfg.node_id),
            clicks=ingestor.sink,
            limiter=RateLimiter(),
        )
        log.info(
            "ready",
            node_id=cfg.node_id,
            base_url=cfg.base_url,
            api_keys=len(cfg.api_keys),
            dashboard="built" if app.state.dashboard_dist else "not built",
        )

        try:
            yield
        finally:
            outcome = await drain_ingestor(ingestor, ingest_task)
            await redis.aclose()
            await pool.close()
            log.info("shutdown complete", ingest=outcome.value)

    app = FastAPI(
        title="url-shortener",
        summary="Coordination-free ids, a stampede-proof cache, async click ingestion.",
        lifespan=lifespan,
    )
    app.state.dashboard_dist = dashboard_dist()

    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)

    # Registration order is the routing order, and `/{slug}` in redirect_router
    # matches any single segment - so it goes last, after every fixed path.
    app.include_router(public_router)
    app.include_router(protected_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    if app.state.dashboard_dist is not None:
        app.mount(
            "/assets",
            StaticFiles(directory=app.state.dashboard_dist / "assets", check_dir=False),
            name="assets",
        )
    app.include_router(redirect_router)
    return app


def _redact(url: str) -> str:
    """Strip credentials from a connection URL before it reaches a log line."""
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info("starting", addr=f"0.0.0.0:{cfg.port}", hint="POST /api/links to shorten")
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
        # access log is off because RequestIdMiddleware already emits one
        # structured line per request - two would just double the I/O.
        loop="auto",
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
