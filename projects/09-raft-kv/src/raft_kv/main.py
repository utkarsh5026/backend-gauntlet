"""Distributed key-value store on Raft — entrypoint and wiring.

The plumbing (config, the cluster topology from env, the node, the peer RPC
transport, the FastAPI app, the driver task, graceful shutdown) is wired up for
you. The learning lives in the modules marked `TODO(Vx)`: leader election (V1,
`election.py`), log replication + commit (V2, `replication.py`), the replicated
KV state machine (V3, `store.py`), and snapshots + compaction (V4,
`snapshot.py`). See SPEC.md.

A cluster is N of these processes, each with a distinct `NODE_ID` and a shared
`PEERS` map. There is no external dependency — each node persists its own state to
disk and reaches the others over HTTP. Scaffold state: this starts and serves.
`GET /healthz`, `GET /status` and `GET /metrics` work; the node idles as a
follower (the driver is a scaffold — see `node.RaftNode.run`), and the first
client write or inbound RPC raises `NotImplementedError`. That message is your
worklist.

## Run a 3-node cluster locally

```bash
make cluster                                   # all three, one terminal
# ...or three terminals:
NODE_ID=1 make run
NODE_ID=2 make run
NODE_ID=3 make run
```

## Where graceful shutdown actually happens

The `finally` half of the lifespan. By the time it runs, uvicorn has stopped
accepting connections and let in-flight requests finish — so what is left is to
stop the driver and flush the persistent state. The ordering matters and is the
horizontal checklist item: **cancel the driver first, then persist.** A driver
still running while you write the term/vote/log is a second writer, and the
restart that follows would find whatever the interleaving happened to leave.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Settings
from .errors import install_error_handlers
from .log import RaftLog
from .node import RaftConfig, RaftNode
from .peer import PeerClient
from .routes import router
from .state import AppState
from .store import Store

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for the driver task to stop before giving up on it.

Short on purpose: the driver's own work is timers and peer RPCs, all of which are
already bounded, so anything slower than this is stuck rather than busy."""


def raft_config(cfg: Settings) -> RaftConfig:
    """Translate settings into the node's timing config."""
    return RaftConfig(
        heartbeat_interval=cfg.heartbeat_interval,
        election_timeout_min=cfg.election_timeout_min,
        election_timeout_max=cfg.election_timeout_max,
        snapshot_threshold=cfg.snapshot_threshold,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct several
    independent nodes in one process, each with its own data directory and peer
    map. That matters more here than in most projects: a meaningful test of this
    system *is* a multi-node cluster, and two nodes sharing a `data_dir` would be
    two writers on one node's persistent state.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Opens (and on restart, recovers) this node's persistent term/vote/log.
        # Recovery is V1/V2 work — see `RaftLog.open`.
        raft_log = RaftLog.open(cfg.state_path)
        peer_client = PeerClient(cfg.peer_addrs, timeout=cfg.peer_timeout)
        node = RaftNode(
            node_id=cfg.node_id,
            config=raft_config(cfg),
            self_addr=cfg.self_addr,
            peer_addrs=cfg.peer_addrs,
            log=raft_log,
            store=Store(),
            peer_client=peer_client,
        )
        app.state.app_state = AppState(settings=cfg, node=node)

        # Held in a local for the whole lifespan: a bare `create_task` result that
        # nobody keeps can be garbage-collected mid-flight, which here would mean
        # the node silently stops running consensus while still answering
        # `/healthz` — the worst possible failure mode to debug.
        driver = asyncio.create_task(node.run(), name="raft-driver")

        logger.info(
            "raft node initialized",
            node=node.id,
            cluster_size=node.cluster_size,
            quorum=node.quorum,
            self_addr=node.self_addr,
            state_path=str(cfg.state_path),
        )
        try:
            yield
        finally:
            # Stop the clock before touching the persistent state — see the
            # module docstring on shutdown ordering.
            driver.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_BUDGET):
                    await driver
            except (TimeoutError, asyncio.CancelledError):
                pass

            try:
                await raft_log.persist()
            except NotImplementedError as exc:
                # V1/V2 durability is not built yet. Report it, but never let an
                # unwritten flush turn a clean shutdown into a crash.
                logger.warning("persistent state flush is still a todo", detail=str(exc))

            await peer_client.aclose()
            logger.info("shutdown complete")

    app = FastAPI(
        title="raft-kv",
        summary="A distributed key-value store built on Raft from scratch (project 09).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id,
    # including the ones from a peer RPC handler — which is what lets you follow
    # one write across three nodes' logs.
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
        node=cfg.node_id,
        addr=f"0.0.0.0:{cfg.bind_port}",
        hint="PUT /kv/{key} to write, GET /status to watch the cluster",
    )
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        # Derived from this node's own entry in PEERS, so one list defines the
        # whole topology and a node cannot listen somewhere its peers aren't
        # calling.
        port=cfg.bind_port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
        # access log is off because RequestIdMiddleware already emits one
        # structured line per request — two would just double the I/O.
        loop="auto",
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
