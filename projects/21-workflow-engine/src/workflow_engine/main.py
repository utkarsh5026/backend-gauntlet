"""Workflow engine (Temporal-lite) — entrypoint and wiring.

The plumbing is done for you: config, telemetry, the Postgres pool, the gRPC
server with health checking and reflection, the admin HTTP port, the durable
timer scan loop, and a graceful shutdown that drains in-flight RPCs *and* lets
the timer scan finish its pass. The learning lives in the modules marked
`TODO(Vx)`:

  - V1 `history.py`  — the append-only event log (the state IS the log).
  - V2 `replay.py`   — folding a history into state, deterministically.
  - V3 `timers.py`   — durable timers that survive a restart.
  - V4 `dispatch.py` — task queues, long-poll, at-least-once worker dispatch.
  - V5 `sticky.py`   — worker-affinity cache that skips full replay.

See SPEC.md.

Scaffold state: this starts and serves. `grpcurl` can list the service through
reflection, `grpc.health.v1/Check` answers SERVING, and `GET /healthz` and
`GET /metrics` work. Edge validation is real, so a malformed request is refused
properly. The first *valid* RPC raises `NotImplementedError` from the engine
underneath — that is your worklist.

**One loop, two servers.** The gRPC server and the admin HTTP server share a
single event loop rather than running the admin side in a thread. That is the
honest arrangement: a blocking call anywhere shows up as *both* going quiet,
which is the feedback you want while you are being graded on "no blocking call on
the event loop". uvicorn is constructed with `loop="none"` because the loop
already exists — letting it install its own would fight `uvloop.run` for it.
"""

from __future__ import annotations

import asyncio
import signal

import asyncpg
import common_telemetry
import grpc
import grpc.aio
import structlog
import uvicorn
import uvloop
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from .config import Settings
from .db import create_pool
from .dispatch import Dispatcher
from .history import HistoryStore
from .pb import workflow_pb2 as pb
from .pb import workflow_pb2_grpc as rpc
from .routes import create_admin_app
from .service import WorkflowService
from .state import AppState
from .sticky import StickyCache
from .timers import TimerService, scan_loop

log = structlog.get_logger(__name__)

SERVICE_NAME = pb.DESCRIPTOR.services_by_name["WorkflowService"].full_name

SHUTDOWN_GRACE_SECONDS = 5.0
"""How long a stopping server waits for in-flight RPCs before cutting them off.

Long enough to finish work already accepted, short enough that a rolling deploy
doesn't stall. Note what it is *not* sized for: parked long-polls. Those are told
to return early via `Dispatcher.shutdown` rather than waited out, because a
five-second grace against a five-second poll window would make every deploy a
coin flip.
"""


def build_state(settings: Settings, pool: asyncpg.Pool[asyncpg.Record]) -> AppState:
    """Assemble the engine over an existing pool.

    Separate from `serve` (and from opening the pool) so tests can build the same
    object graph without a database — every engine method raises before it would
    touch one.
    """
    history = HistoryStore(pool)
    timers = TimerService(pool)
    sticky = StickyCache(settings.sticky_ttl)
    dispatcher = Dispatcher(pool, history, timers, sticky, settings)
    return AppState(
        settings=settings,
        pool=pool,
        history=history,
        timers=timers,
        sticky=sticky,
        dispatcher=dispatcher,
    )


def build_grpc_server(state: AppState) -> tuple[grpc.aio.Server, health.HealthServicer]:
    """Construct the gRPC server with the engine, health, and reflection on it."""
    # grpc-stubs types `aio.server`'s handler/interceptor generics as bare, so
    # pyright can't infer them at the call site. The returned Server is typed.
    server = grpc.aio.server(  # pyright: ignore[reportUnknownMemberType]
        # Backpressure, not an OOM: past this many in-flight RPCs the server
        # queues rather than accepting unbounded work. Long-poll makes the number
        # subtler than usual — every parked poller is an in-flight RPC — so it
        # has to exceed the fleet's worker count with room to spare.
        maximum_concurrent_rpcs=state.settings.max_concurrent_rpcs,
    )
    rpc.add_WorkflowServiceServicer_to_server(WorkflowService(state.dispatcher), server)

    # grpc.health.v1 — what a gRPC client-side load balancer checks to decide
    # whether to keep sending this instance traffic.
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    # Server reflection, so `grpcurl` can introspect the service without being
    # handed a copy of the .proto.
    reflection.enable_server_reflection(
        (
            SERVICE_NAME,
            health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
            reflection.SERVICE_NAME,
        ),
        server,
    )
    return server, health_servicer


def set_health(servicer: health.HealthServicer, *, serving: bool) -> None:
    """Flip the health status for the whole server and for this service.

    The empty service name is the gRPC convention for "the server as a whole",
    which is what a client-side load balancer asks about; the named entry is for
    a caller that wants this specific service. Both, always, or a balancer and a
    client can disagree about whether this instance is taking traffic.
    """
    status = (
        health_pb2.HealthCheckResponse.SERVING
        if serving
        else health_pb2.HealthCheckResponse.NOT_SERVING
    )
    for name in ("", SERVICE_NAME):
        # grpcio-health-checking ships no stubs for its own generated
        # `health_pb2`, so the ServingStatus enum arrives as Unknown.
        servicer.set(name, status)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


async def serve(settings: Settings) -> None:
    """Run both servers (and the timer loop) until a signal arrives, then drain."""
    pool = await create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    log.info("connected to postgres", pool_min=settings.db_pool_min, pool_max=settings.db_pool_max)

    state = build_state(settings, pool)
    server, health_servicer = build_grpc_server(state)
    server.add_insecure_port(f"0.0.0.0:{settings.port}")

    admin = uvicorn.Server(
        uvicorn.Config(
            create_admin_app(state),
            host="0.0.0.0",
            port=settings.metrics_port,
            # The loop is already running under us; uvicorn must not install one.
            loop="none",
            # A Prometheus scrape every 15s does not need a log line each time.
            access_log=False,
            log_config=None,
        )
    )

    await server.start()
    admin_task = asyncio.create_task(admin.serve(), name="admin-http")

    # The durable-timer scan loop runs only when asked, so the bare scaffold
    # serves cleanly. (Its first pass hits a V3 `NotImplementedError` — flip
    # RUN_TIMER_SERVICE=true once V3 works.)
    timer_task: asyncio.Task[None] | None = None
    if settings.run_timer_service:
        timer_task = asyncio.create_task(
            scan_loop(
                state.timers,
                state.dispatcher,
                settings.timer_scan_interval,
                settings.timer_scan_batch,
                state.dispatcher.shutdown,
            ),
            name="timer-scan",
        )
        log.info("durable timer service started")
    else:
        log.info("durable timer service disabled (RUN_TIMER_SERVICE=false)")

    # Only now do we report SERVING.
    set_health(health_servicer, serving=True)

    log.info(
        "workflow engine listening",
        grpc_addr=f"0.0.0.0:{settings.port}",
        admin_addr=f"0.0.0.0:{settings.metrics_port}",
        long_poll_ms=settings.long_poll_timeout_ms,
        visibility_timeout_ms=settings.task_visibility_timeout_ms,
        sticky_ttl_ms=settings.sticky_ttl_ms,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler, not signal.signal: the handler runs as a loop
        # callback rather than interrupting arbitrary bytecode, so it can touch
        # asyncio objects safely. SIGTERM is the one that matters — it is what
        # `docker stop` and Kubernetes send.
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutdown signal received", grace_seconds=SHUTDOWN_GRACE_SECONDS)
    # NOT_SERVING first, then drain: a load balancer that is watching gets to
    # stop sending new work before the door closes on the work already inside.
    set_health(health_servicer, serving=False)

    # Wake every parked long-poll so it returns "no work" instead of being cut
    # off holding a claim, and tell the scan loop to stop after its current pass.
    state.dispatcher.shutdown.set()

    admin.should_exit = True
    await server.stop(SHUTDOWN_GRACE_SECONDS)
    if timer_task is not None:
        await timer_task
    await admin_task
    await pool.close()
    log.info("shutdown complete")


def main() -> None:
    settings = Settings()
    common_telemetry.init(settings.log_level)
    log.info(
        "starting",
        hint=f"grpcurl -plaintext localhost:{settings.port} list",
    )
    # uvloop is the production loop. It is materially faster on the socket work
    # a long-polled engine spends its life doing — and it is also where
    # implementation gaps hide, so run it here rather than discovering the
    # difference in prod.
    uvloop.run(serve(settings))


if __name__ == "__main__":
    main()
