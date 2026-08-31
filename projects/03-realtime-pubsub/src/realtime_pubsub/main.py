"""Real-time pub/sub + presence — entrypoint and wiring.

The plumbing (config, telemetry, the hub/presence registries, the presence sweep
task, the optional Redis cluster bridge, the optional roster DB, the FastAPI app,
graceful shutdown) is wired for you. The learning lives in the module marked
`TODO(V4)`: the multi-node bus (`cluster.py`). See SPEC.md.

Scaffold state: this starts and serves. `GET /healthz`, `GET /metrics`, the
`/admin` roster and the whole single-node WebSocket path (V1-V3) work. Turn on
`CLUSTER=true` and a publish raises V4's `NotImplementedError` — that traceback
is the worklist.

**Degraded start is deliberate.** A roster DB that is down at boot logs a warning
and leaves the app serving: `/admin` answers 503 while the pub/sub core — which
is store-free by design — is completely unaffected. A crash-loop tells an
orchestrator nothing; a process that is up and visibly refusing one endpoint
tells it everything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import directory as directory_module
from .cluster import ClusterBridge
from .config import Settings
from .directory import Directory
from .errors import install_error_handlers
from .hub import Hub
from .presence import PresenceRegistry
from .protocol import PresenceMessage
from .routes import router
from .state import AppState

log = structlog.get_logger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "migrations" / "0001_directory.sql"


async def sweep_presence(
    hub: Hub,
    presence: PresenceRegistry,
    interval: float,
    ttl: float,
) -> None:
    """Background loop that reaps stale presence and fans out the survivors.

    Every `interval`, drop members whose last heartbeat is older than `ttl`
    (silent TCP drops that never sent a leave). For each topic that actually
    changed, publish a `presence` frame so still-connected subscribers see the
    updated roster without waiting for their next join/leave.

    Note what this loop does *not* do: sleep for `interval` and then work. It
    measures from the top of each tick, so a sweep that takes 200ms does not
    push the next one 200ms late, and the cadence stays honest as rooms grow.
    """
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    while True:
        next_tick += interval
        await asyncio.sleep(max(0.0, next_tick - loop.time()))
        for topic, members in presence.sweep(ttl):
            hub.publish(topic, PresenceMessage(topic=topic, members=[m.identity for m in members]))


def _log_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead background task the moment it dies.

    An asyncio task that raises does so *silently*: nothing is printed until
    someone awaits it or the garbage collector complains. For the presence sweep
    or the cluster bridge, that silence is indistinguishable from working — you
    would watch a room fill with ghosts for ten minutes before noticing. This
    callback is the fix, and it is the habit to keep for every long-lived task
    you ever spawn.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "background task died",
            task=task.get_name(),
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


async def _connect_directory(cfg: Settings) -> Directory | None:
    """Open the roster DB, or `None` if it is disabled or unreachable."""
    if not cfg.admin_enabled:
        log.info("directory: DATABASE_URL unset — /admin roster API disabled")
        return None
    try:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        dir_handle = await directory_module.connect(cfg.database_url, schema)
        log.info("directory: connected to postgres, schema applied")
        return dir_handle
    except Exception as exc:  # noqa: BLE001 - degraded start beats a crash loop
        log.warning(
            "directory unreachable; /admin will 503",
            error=str(exc) or type(exc).__name__,
        )
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent instance (its own hub, its own settings) without touching the
    environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        hub = Hub()
        presence = PresenceRegistry()

        cluster: ClusterBridge | None = None
        if cfg.cluster:
            cluster = ClusterBridge(cfg.redis_url, cfg.node_id, hub)
            log.info("cluster mode: bridged to redis bus", node_id=cfg.node_id)
        else:
            log.info("single-node mode (CLUSTER=false): redis bus not used")

        app.state.app_state = AppState(
            settings=cfg,
            hub=hub,
            presence=presence,
            cluster=cluster,
            directory=await _connect_directory(cfg),
        )

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(
                sweep_presence(
                    hub,
                    presence,
                    cfg.presence_sweep_interval_secs,
                    cfg.presence_ttl_secs,
                ),
                name="presence-sweep",
            )
        ]
        if cluster is not None:
            tasks.append(asyncio.create_task(cluster.run(), name="cluster-bridge"))
        for task in tasks:
            task.add_done_callback(_log_task_exit)

        if not cfg.ws_auth_token.get_secret_value():
            log.warning("WS_AUTH_TOKEN is not set — every websocket upgrade will be rejected")

        try:
            yield
        finally:
            # Ordering is the graceful-shutdown contract. Uvicorn has already
            # stopped accepting and has closed live WebSockets by the time this
            # runs, so every connection's `finally` has fired and the hub is
            # empty. Now stop the background work and release the connections.
            for task in tasks:
                task.cancel()
            # `gather` with `return_exceptions` so one task's failure cannot
            # stop us from awaiting (and therefore cleanly retiring) the others.
            await asyncio.gather(*tasks, return_exceptions=True)

            state = cast_state(app)
            if state.cluster is not None:
                await state.cluster.aclose()
            if state.directory is not None:
                await state.directory.aclose()
            log.info("shutdown complete")

    app = FastAPI(
        title="realtime-pubsub",
        summary="WebSocket fan-out, backpressure, presence, and a cross-node bus (project 03).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def cast_state(app: FastAPI) -> AppState:
    state: AppState = app.state.app_state
    return state


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info(
        "starting",
        addr=f"0.0.0.0:{cfg.port}",
        overflow_policy=cfg.overflow_policy.value,
        outbox_capacity=cfg.outbox_capacity,
        hint="websocket at /ws",
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
        # Send a proper close frame to live sockets on SIGTERM instead of
        # yanking the TCP connection, and bound how long we wait for them.
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
