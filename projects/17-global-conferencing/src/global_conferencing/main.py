"""Global WebRTC conferencing (cascaded SFU) — entrypoint and wiring.

This is a **capstone**: no new media primitive. One process is **one regional
SFU**. It composes the region-local SFU from project 15 (ICE/STUN, per-subscriber
RTP rewrite, simulcast selection, BWE) and the transport under it (project 14)
into a *global* cascade, leaning on project 09's Raft for room placement. The
plumbing — config, telemetry, metrics, both UDP planes, the backbone pump, the
cluster RPC client, the signaling + cluster + admin HTTP server, graceful
shutdown — is wired. The learning lives in the modules marked `TODO(Vx)`:

* V1 `placement.py` — global room placement via consensus (one home region per room, ever)
* V2 `cascade.py`   — the inter-SFU relay mesh (one copy per region pair, loop-free)
* V3 `routing.py`   — cross-region simulcast routing (each leg carries the union of demand)
* V4 `recording.py` — the recorder as a durable subscriber to the cascade

Scaffold state: this starts and serves. `/healthz`, `/readyz`, `/status`,
`GET /rooms` and `/metrics` answer immediately. The first `publish` tries to
*place* the room and answers `501` naming V1's `place_room`; the first datagram
on the backbone port reaches V2's `on_relayed` and ends the pump, turning
`/readyz` red. The election loop is gated behind `RUN_BACKGROUND=false` so the
bare scaffold does not drive a consensus round with no peers.

## Shutdown order is part of the SPEC

`uvicorn.run` owns SIGTERM: it stops accepting connections (no new participants
admitted), drains in-flight signaling and cluster requests, and *then* runs the
lifespan's `finally`. That `finally` is ordered, and the order is the graceful-
shutdown checklist item:

1. **Stop the election loop** — or it may re-elect this node after it steps down.
2. **Relinquish leadership** (V1) — while the cluster client is still open, so
   the mesh re-elects in a heartbeat instead of a full election timeout.
3. **Tear down relay legs** (V2) — while the backbone socket is still open.
4. **Stop the backbone pump** — no more relay copies in.
5. **Finalize recordings** (V4) — nothing is still writing to them.
6. Close the sockets and the HTTP client (the exit stack, last in first out).

Steps 2, 3 and 5 are vertical code. On the bare scaffold each logs that it is not
built yet and shutdown carries on — a missing vertical must never turn a clean
stop into a failed one.

## What CPython costs you, stated up front

The Hairpin asks for a per-region forwarding p99 ≤ 10 ms and flat RSS across a
five-minute join storm, with three SFUs relaying to each other. Those numbers are
not scaled down for Python. The candidates for the gap are known in advance: the
backbone pump, the media fan-out, the signaling API and the election loop all
share one thread per SFU; a `json` round-trip per heartbeat per peer is
interpreter work on that thread; and V4's disk writes are blocking calls waiting
to land on the loop. "Python is slow" is not a finding —
`docs/17-benchmarks.md` names which of those the flamegraph shows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import admin, metrics, routes
from .cascade import CascadeConfig, CascadeMesh
from .cluster import ClusterClient
from .config import Settings
from .errors import install_error_handlers
from .placement import Placement, PlacementConfig
from .recording import Recorder, RecordingConfig
from .routing import LayerRouter, RoutingConfig
from .state import AppState
from .transport import open_udp_endpoint, run_backbone

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 15
"""How long uvicorn lets in-flight HTTP finish after SIGTERM, before the lifespan
teardown. Budgeted against a k8s grace period of 30 s: signaling is fast, but the
teardown that follows makes RPCs to other continents."""

TASK_STOP_BUDGET = 5.0
"""Seconds to wait for a cancelled background task. They hold nothing, so longer
means one is wedged — typically a broad `except` swallowing `CancelledError`."""

SHUTDOWN_STEP_BUDGET = 5.0
"""Seconds each vertical's shutdown step gets. A peer on the far side of a
partition must not hold the whole stop hostage."""


def _log_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead background task the moment it dies.

    A task that raises does so *silently* until someone awaits it. For the task
    that drains the backbone, that silence looks exactly like a quiet mesh — you
    would stare at flat relay counters for ten minutes. Keep this habit for every
    long-lived task you spawn.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background task died",
            task=task.get_name(),
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


async def _stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        async with asyncio.timeout(TASK_STOP_BUDGET):
            await task
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception as exc:
        # Already died (a vertical's NotImplementedError); reported by
        # `_log_task_exit`. Never let it fail the shutdown.
        logger.debug("task had already ended with an error", task=task.get_name(), error=str(exc))


async def _shutdown_step(label: str, step: Callable[[], Awaitable[None]]) -> None:
    try:
        async with asyncio.timeout(SHUTDOWN_STEP_BUDGET):
            await step()
    except NotImplementedError as exc:
        logger.warning("shutdown step not built yet", step=label, todo=str(exc))
    except TimeoutError:
        logger.warning(
            "shutdown step overran its budget", step=label, budget_s=SHUTDOWN_STEP_BUDGET
        )
    except Exception as exc:
        logger.warning("shutdown step failed", step=label, error=str(exc), kind=type(exc).__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app, with both planes and the verticals on its lifespan.

    A factory, not a module-level `app`, so a test can build three SFUs with
    different `REGION`s on ephemeral ports in one process — a whole mesh, no
    second machine.
    """
    config = settings if settings is not None else Settings()
    peers = config.peer_nodes
    metrics.preregister(config.region, (peer.region for peer in peers))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        routing = LayerRouter(RoutingConfig(hysteresis_ticks=config.hysteresis_ticks))
        cascade = CascadeMesh(
            CascadeConfig(region=config.region, peers=peers, max_legs=config.max_relay_links),
            routing,
        )
        recorder = Recorder(
            RecordingConfig(directory=config.recording_dir, segment_secs=config.segment_secs)
        )

        async with AsyncExitStack() as stack:
            cluster = ClusterClient(peers, timeout=config.cluster_rpc_timeout)
            stack.push_async_callback(cluster.aclose)
            placement = Placement(
                PlacementConfig(
                    region=config.region,
                    node_id=config.node_id,
                    peers=peers,
                    election_timeout=config.election_timeout,
                    heartbeat=config.heartbeat,
                    max_rooms=config.max_rooms,
                ),
                cluster,
            )

            backbone = await stack.enter_async_context(
                open_udp_endpoint("cascade", config.cascade_port, config.cascade_inbox)
            )
            # Project 15's media pump mounts on this endpoint during integration;
            # the scaffold holds the port so the advertised candidate is real.
            media = await stack.enter_async_context(
                open_udp_endpoint("media", config.media_port, config.media_inbox)
            )

            # Held in locals and on AppState for the whole lifespan: an unreferenced
            # `create_task` result can be garbage-collected mid-flight.
            pump = asyncio.create_task(run_backbone(backbone, cascade), name="backbone-pump")
            pump.add_done_callback(_log_task_exit)
            election: asyncio.Task[None] | None = None
            if config.run_background:
                election = asyncio.create_task(placement.run(), name="placement-election")
                election.add_done_callback(_log_task_exit)

            app.state.app_state = AppState(
                settings=config,
                placement=placement,
                cascade=cascade,
                routing=routing,
                recorder=recorder,
                cluster=cluster,
                media=media,
                backbone=backbone,
                backbone_pump=pump,
                election=election,
            )
            logger.info(
                "regional SFU up",
                region=config.region,
                node_id=config.node_id,
                peers=[peer.region for peer in peers],
                quorum=placement.config.quorum,
                background=config.run_background,
            )
            try:
                yield
            finally:
                # The order is the SPEC's graceful-shutdown item — see the module docstring.
                if election is not None:
                    await _stop_task(election)
                await _shutdown_step("relinquish leadership (V1)", placement.step_down)
                await _shutdown_step("tear down relay legs (V2)", cascade.close_all)
                await _stop_task(pump)
                await _shutdown_step("finalize recordings (V4)", recorder.flush_all)
                logger.info("shutdown complete")

    app = FastAPI(
        title="global-conferencing",
        summary="A cascaded multi-region SFU: placement, relay mesh, routing, recording (17).",
        lifespan=lifespan,
    )
    # TODO(observability): the SPEC asks for context per participant and per relay
    # leg. `structlog.contextvars.bind_contextvars(region=..., room=...)` inside a
    # handler rides along on every line that request emits.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(admin.router)
    app.include_router(routes.router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    config = Settings()
    common_telemetry.init(config.log_level)
    logger.info(
        "starting",
        region=config.region,
        http_addr=f"0.0.0.0:{config.http_port}",
        media_addr=f"0.0.0.0:{config.media_port}",
        cascade_addr=f"0.0.0.0:{config.cascade_port}",
        hint=f"curl -XPOST localhost:{config.http_port}/rooms/all-hands/publish -d '{{...}}'",
    )
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.http_port,
        # "auto" picks uvloop (installed by uvicorn[standard]) — not the loop
        # pytest runs on. That is why transport.py uses datagram endpoints.
        loop="auto",
        # RequestIdMiddleware already logs one structured line per request.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
