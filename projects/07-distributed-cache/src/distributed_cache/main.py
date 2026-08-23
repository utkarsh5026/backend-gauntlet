"""Distributed cache — entrypoint and wiring.

The plumbing (config, the local store, the gossip socket, the FastAPI app,
graceful shutdown) is wired for you. The learning lives in the modules marked
`TODO(Vx)`: the bounded LRU/LFU store (V1, `store.py`), the consistent-hash ring
(V2, `ring.py`), SWIM gossip membership (V3, `membership.py`), and replication +
request coordination (V4, `coordinator.py`). See SPEC.md.

Each node is one instance of this app; a "cluster" is several of them that find
each other via gossip seeds. Scaffold state: this starts and serves.
`GET /healthz`, `GET /cluster` and `GET /metrics` work (the node sees itself);
the first real `GET`/`PUT /cache/...` raises a NotImplementedError — that is your
worklist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import httpx
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Settings
from .coordinator import Coordinator
from .errors import install_error_handlers
from .membership import Membership
from .routes import internal_router, public_router
from .state import AppState
from .store import Store

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent node (its own store, its own gossip port) without touching the
    environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        store = Store(cfg.cache_capacity, cfg.eviction_policy)

        # Binds the gossip UDP socket and seeds the view with this node.
        membership = await Membership.bind(cfg.bind_node, cfg.seeds, cfg.vnodes_per_node)

        # One client for the process. The limits are deliberate and graded:
        # an unbounded pool turns a slow peer into unbounded memory, and no
        # timeout turns one hung peer into a stuck event loop.
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(2.0, connect=1.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

        coordinator = Coordinator(cfg.node_id, store, membership, cfg.replication_factor, http)
        app.state.app_state = AppState(
            settings=cfg, store=store, membership=membership, coordinator=coordinator
        )

        log.info(
            "local store ready",
            node_id=cfg.node_id,
            capacity=cfg.cache_capacity,
            policy=str(cfg.eviction_policy),
            vnodes=cfg.vnodes_per_node,
            replication_factor=cfg.replication_factor,
            seeds=[str(s) for s in cfg.seeds],
        )

        # Drives SWIM in the background (receive loop now; the probe ticker is a
        # V3 TODO). Held in a local so it is never garbage-collected mid-flight.
        gossip = asyncio.create_task(membership.run(), name="swim-gossip")
        try:
            yield
        finally:
            # TODO(V3 / graceful shutdown): before tearing the socket down,
            # gossip this node's departure (broadcast it as leaving) so peers
            # drop it immediately instead of waiting a full suspicion timeout.
            gossip.cancel()
            try:
                await gossip
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                # The gossip task died earlier (a V3 NotImplementedError while
                # you build it, say). Report it, but never let it turn a clean
                # shutdown into a failed one — in-flight requests still drain.
                log.warning("gossip task ended with an error", error=str(exc))
            await http.aclose()
            membership.close()
            log.info("shutdown complete", node_id=cfg.node_id)

    app = FastAPI(
        title="distributed-cache",
        summary="A sharded, replicated, gossip-clustered cache (project 07).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(public_router)
    app.include_router(internal_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info(
        "starting",
        http_addr=f"0.0.0.0:{cfg.port}",
        gossip_addr=f"0.0.0.0:{cfg.gossip_port}",
        hint="PUT /cache/{key} to store; GET /cluster for membership",
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
