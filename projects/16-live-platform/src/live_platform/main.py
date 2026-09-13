"""Live streaming platform (Twitch-lite) — entrypoint and wiring.

This is the **capstone**: no new primitive, it composes the pieces built earlier
into one glass-to-glass pipeline — ingest → ABR transcode ladder → LL-HLS
packaging → edge delivery → realtime chat — and runs it on k8s with autoscaling
transcode workers. The plumbing is wired for you: config, telemetry, the
Postgres pool, the Redis bus, the NATS connection, the origin HTTP client, the
FastAPI app, and graceful shutdown. The learning lives in four modules:

* V1 `control.py` — the stream session state machine and reconciliation
* V2 `workers.py` — the transcode queue, the lease, and the autoscale signal
* V3 `edge.py`    — LL-HLS blocking reload and single-flight segment fan-out
* V4 `chat.py`    — per-channel fan-out, backpressure, presence, cross-pod bus

Scaffold state: this starts and serves once `make up` has the three deps running.
`/healthz`, `/readyz`, `/status` and `/metrics` answer immediately. The ingest,
playback and chat routes exist and answer `501` naming their todo. The background
loops (reconcile, queue setup, the chat bus) call into V1/V2/V4, so they are gated
behind `RUN_BACKGROUND=false` — flip it on once those verticals exist.

## One app, and the lifespan owns every connection

Every connection is opened on an `AsyncExitStack` inside the lifespan, so they
close in reverse order of opening whichever way startup or shutdown goes: the
origin client first, then NATS (drained, so in-flight publishes and acks are
flushed rather than dropped), then Redis, then the Postgres pool. If Postgres is
up but NATS is not, startup fails *and* the pool that did open is closed.

`uvicorn.run` installs the SIGTERM handler, so `kubectl delete pod` or a rolling
deploy triggers uvicorn's graceful shutdown: it stops accepting connections,
lets in-flight requests finish, and *then* runs the lifespan's `finally`.

## What CPython costs you here, stated up front

The Viral Spike asks for glass-to-glass p95 ≤ 3 s through a 200 → 100k viewer
ramp, ≤ 1 origin fill under a 1,000-viewer stampede, and chat p99 fan-out
≤ 500 ms to 100k subscribers. Those numbers are **not** scaled down for Python.
Where CPython cannot reach one, the gap is the finding, recorded with its cause
in `docs/16-benchmarks.md`.

Every held-open playlist reload, every chat socket and every origin fill in this
process shares **one** thread. A fan-out loop over 100k outboxes that takes
200 ms is 200 ms during which no playlist reload returns and no ingest webhook is
answered. "Python is slow" is not a finding; "the chat fan-out holds the loop for
180 ms per message at 100k subscribers, which lands directly in playlist latency"
is one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import common_telemetry
import httpx
import nats
import structlog
import uvicorn
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient
from redis.asyncio import Redis

from . import db, metrics
from .admin import router as admin_router
from .chat import ChatHub
from .config import Settings
from .control import ControlPlane
from .edge import EdgeCache
from .errors import install_error_handlers
from .routes import router as app_router
from .state import AppState, task_failure
from .workers import WorkerPool

logger = structlog.get_logger(__name__)

__all__ = ["build_state", "create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 25
"""How long uvicorn lets in-flight requests finish after SIGTERM.

Chosen against k8s' default `terminationGracePeriodSeconds` of 30: after that,
the kubelet sends SIGKILL and nothing in the `finally` runs. Twenty-five leaves
five seconds for the lifespan teardown. A held LL-HLS reload or an open chat
socket counts as in flight, so this budget is really spent on those."""

BACKGROUND_SHUTDOWN_BUDGET = 3.0
"""Seconds to wait for a cancelled background task to actually stop."""

ORIGIN_TIMEOUT_SECONDS = 5.0
"""Default timeout for origin fills. A placeholder: the fill deadline is a V3
decision (a viewer is waiting on it, and so are the requests coalesced behind it),
so treat this as the number to replace, not the number to keep."""


def build_state(
    settings: Settings,
    *,
    pool: db.asyncpg.Pool[db.asyncpg.Record],
    redis: Redis,
    nats: NatsClient,
    http: httpx.AsyncClient,
) -> AppState:
    """Assemble the four planes over already-constructed handles. No I/O.

    Split out from the lifespan so the tests can build the whole platform over
    handles that never connect — asyncpg's `create_pool()` returns an unconnected
    pool until awaited, `Redis.from_url` dials lazily, and an unconnected NATS
    client still hands out a JetStream context. The scaffold suite runs in CI
    that way, with no services at all.
    """
    return AppState(
        settings=settings,
        pool=pool,
        redis=redis,
        nats=nats,
        http=http,
        platform=ControlPlane(
            pool,
            ladder=settings.ladder,
            max_streams=settings.max_streams,
            segment_secs=settings.segment_secs,
            part_secs=settings.part_secs,
        ),
        workers=WorkerPool(
            # `jetstream(**opts)` is untyped in nats-py; no options are passed.
            nats.jetstream(),  # pyright: ignore[reportUnknownMemberType]
            stream_name=settings.transcode_stream,
            lease_secs=settings.transcode_lease_secs,
            target_backlog_per_worker=settings.target_backlog_per_worker,
            max_replicas=settings.max_transcode_replicas,
        ),
        edge=EdgeCache(http, origin_base=settings.packager_origin),
        chat=ChatHub(redis, node_id=settings.node_id, outbox_capacity=settings.outbox_capacity),
    )


async def _connect(config: Settings, stack: AsyncExitStack) -> AppState:
    """Open every connection, registering each close on `stack` as it opens.

    Nothing here logs a URL: `DATABASE_URL` carries a password, and in production
    so can the Redis and NATS URLs.
    """
    pool = await db.create_pool(config.database_url, min_size=1, max_size=config.db_max_connections)
    stack.push_async_callback(pool.close)
    logger.info("connected to postgres (control plane)")

    # redis-py and nats-py declare their options as bare `**kwargs`, so pyright
    # strict reads these signatures as partially unknown. Each ignore below
    # claims only that — every argument actually passed is a plain `str`.
    redis = Redis.from_url(config.redis_url)  # pyright: ignore[reportUnknownMemberType]
    stack.push_async_callback(redis.aclose)
    # `from_url` dials lazily. Ping now, so a wrong REDIS_URL fails the boot
    # rather than the first chat message a viewer sends.
    await redis.ping()  # pyright: ignore[reportUnknownMemberType]
    logger.info("connected to redis (chat bus)")

    nats_client = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
        config.nats_url
    )
    stack.push_async_callback(nats_client.drain)
    logger.info("connected to nats (transcode queue)")

    http = httpx.AsyncClient(timeout=ORIGIN_TIMEOUT_SECONDS)
    stack.push_async_callback(http.aclose)

    return build_state(config, pool=pool, redis=redis, nats=nats_client, http=http)


def _log_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead background task the moment it dies.

    A task that raises does so *silently*: nothing is printed until someone
    awaits it or the garbage collector complains. For the chat bus, that silence
    looks exactly like a quiet chat — cross-pod messages simply stop. This
    callback is the fix, and the habit to keep for every long-lived task.
    """
    exc = task_failure(task)
    if exc is not None:
        logger.error(
            "background task died",
            task=task.get_name(),
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


async def _start_background(state: AppState) -> None:
    """Reconcile, ensure the queue, then start the chat bus. Startup-fatal on error.

    `reconcile` and `ensure_queue` run *before* serving and are awaited, so a
    failure aborts the boot — serving playback from a registry that was never
    rebuilt is worse than not serving at all. The bus is long-lived, so it runs
    as a task.
    """
    await state.platform.reconcile()
    await state.workers.ensure_queue()
    bus = asyncio.create_task(state.chat.run_bus(), name="chat-bus")
    bus.add_done_callback(_log_task_exit)
    state.background.append(bus)


async def _stop_background(state: AppState) -> None:
    for task in state.background:
        task.cancel()
    for task in state.background:
        try:
            async with asyncio.timeout(BACKGROUND_SHUTDOWN_BUDGET):
                await task
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            # Already reported by `_log_task_exit`; never let it turn a clean
            # shutdown into a failed one.
            logger.warning(
                "background task ended with an error", task=task.get_name(), error=str(exc)
            )


def _bind_gauges(state: AppState) -> None:
    """Point the read-through gauges at the state they mirror.

    Read at scrape time, so they are never stale and never need updating by
    hand. `desired_replicas` is deliberately *not* bound here: it raises until V2
    exists, and a gauge whose function raises turns every `/metrics` scrape into
    a 500 — including the scrape the HPA depends on.
    """
    metrics.STREAMS_LIVE.set_function(lambda: state.platform.live_count)
    metrics.TRANSCODE_QUEUE_DEPTH.set_function(lambda: state.workers.queue_depth)


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the ASGI app.

    With no `state`, the lifespan opens every connection itself (production).
    With a `state`, it installs that one and opens and closes nothing — the
    caller owns those handles. That is how the tests run the real app over
    handles that never connect.
    """
    config = state.settings if state is not None else (settings or Settings())
    metrics.preregister()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            app_state = state if state is not None else await _connect(config, stack)
            _bind_gauges(app_state)
            app.state.app_state = app_state
            if config.run_background:
                await _start_background(app_state)
            try:
                yield
            finally:
                await _stop_background(app_state)
                logger.info("shutdown complete")

    app = FastAPI(
        title="live-platform",
        summary="Twitch-lite: control plane, transcode autoscale, LL-HLS edge, chat.",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(admin_router)
    app.include_router(app_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    config = Settings()
    common_telemetry.init(config.log_level)
    logger.info(
        "starting",
        http_addr=f"0.0.0.0:{config.port}",
        node_id=config.node_id,
        run_background=config.run_background,
    )
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.port,
        # "auto" picks uvloop, which uvicorn[standard] installs — and which is
        # *not* the loop pytest runs on. That is why the Python checklist asks
        # you to boot the container.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
