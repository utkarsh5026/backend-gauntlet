"""Time-series metrics pipeline — entrypoint and wiring.

The plumbing (config, telemetry, the NATS/JetStream connection + durable stream,
the ClickHouse client, the SSE fan-out hub, the optional consumer pipeline, the
FastAPI app, graceful shutdown) is wired for you. The learning lives in the
modules marked `TODO(Vx)`: the line-protocol parser + series fingerprint (V1,
`parse.py`), the windowed rollup engine with a percentile sketch (V2,
`rollup.py`), the batched at-least-once ClickHouse sink (V3, `sink.py`), and the
SSE live fan-out (V4, `sse.py`). See SPEC.md.

Scaffold state: this starts and serves. `GET /healthz` and `GET /metrics` work;
`POST /ingest` raises the V1 parse todo and `GET /stream` the V4 SSE todo, and
`RUN_CONSUMER=true` makes the pipeline raise on its first rollup. Those
tracebacks are your worklist.

**Degraded start is deliberate.** A broker or store that is down at boot logs a
warning and leaves the app serving — the dependent paths answer 503 while
`/healthz` and `/metrics` still work. A crash-loop tells an orchestrator nothing;
a process that is up and visibly refusing writes tells it everything. (The
readiness half of that split is a TODO in `routes.py`.)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import clickhouse_connect
import common_telemetry
import structlog
import uvicorn
from clickhouse_connect.driver.asyncclient import AsyncClient
from fastapi import FastAPI

from . import broker, pipeline
from .broker import Producer
from .config import Settings
from .errors import install_error_handlers
from .rollup import Rollup
from .routes import router
from .sink import Sink
from .sse import LiveFeed
from .state import AppState

log = structlog.get_logger(__name__)


async def _connect_clickhouse(cfg: Settings) -> AsyncClient | None:
    """Open the ClickHouse handle, or `None` if the store is unreachable.

    `connector_limit` is left at its default here, and that is a knob the SPEC
    asks you to set on purpose rather than inherit: it is the size of the
    connection pool this process will open against ClickHouse, and it wants to be
    reasoned about together with how many concurrent flushes the pipeline can
    actually have in flight (which, in the single-consumer design, is one).
    """
    try:
        # clickhouse-connect's factory ends in **kwargs, which pyright strict reads
        # as partially unknown. Every argument we actually pass is typed.
        return await clickhouse_connect.get_async_client(  # pyright: ignore[reportUnknownMemberType]
            dsn=cfg.clickhouse_url,
            database=cfg.clickhouse_db,
            username=cfg.clickhouse_user,
            password=cfg.clickhouse_password.get_secret_value(),
        )
    except Exception as exc:  # noqa: BLE001 - degraded start beats a crash loop
        log.warning(
            "clickhouse unreachable; /query will fail",
            error=str(exc) or type(exc).__name__,
        )
        return None


def _log_consumer_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead consumer the moment it dies.

    An asyncio task that raises does so *silently*: nothing is printed until
    someone awaits it (here, shutdown) or the garbage collector complains. For a
    background task that IS the pipeline, that silence is indistinguishable from
    working — you would watch an idle `/metrics` for ten minutes before noticing.
    This callback is the fix, and it is the habit to keep for every long-lived
    task you ever spawn.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "consumer pipeline died",
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent instance (its own feed, its own settings) without touching the
    environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # --- Broker: NATS JetStream, the durable log between ingest and consumer.
        nats_client = None
        js = None
        try:
            nats_client = await broker.connect(cfg.nats_url, cfg.broker_connect_timeout)
            # Untyped **opts in nats-py; the returned context is typed.
            js = nats_client.jetstream()  # pyright: ignore[reportUnknownMemberType]
            await broker.ensure_stream(js, cfg.stream_name)
            log.info("connected to NATS JetStream", url=cfg.nats_url, stream=cfg.stream_name)
        except Exception as exc:  # noqa: BLE001 - degraded start, see module doc
            # `str(TimeoutError())` is the empty string, so fall back to the type
            # name — a log line that says `error: ""` tells an operator nothing.
            log.warning(
                "broker unreachable; /ingest will 503",
                error=str(exc) or type(exc).__name__,
            )

        clickhouse = await _connect_clickhouse(cfg)

        # The SSE hub — bounded, so a slow dashboard is shed, not blocking.
        feed = LiveFeed(cfg.sse_capacity)
        app.state.app_state = AppState(
            settings=cfg,
            producer=Producer(js),
            feed=feed,
            clickhouse=clickhouse,
        )

        # The consumer pipeline runs only when asked, so the bare scaffold serves
        # the ingest API cleanly. Its first point hits the V2 rollup todo.
        shutdown = asyncio.Event()
        consumer: asyncio.Task[None] | None = None
        if cfg.run_consumer and js is not None and clickhouse is not None:
            consumer = asyncio.create_task(
                pipeline.run(
                    js,
                    pipeline.PipelineConfig(
                        stream_name=cfg.stream_name,
                        subject=broker.RAW_SUBJECT,
                        durable_name=cfg.durable_name,
                        fetch_batch=cfg.fetch_batch,
                        flush_interval=cfg.flush_interval,
                    ),
                    Rollup(cfg.window, cfg.grace),
                    Sink(clickhouse, cfg.rollup_table, cfg.batch_max_rows, cfg.batch_max_delay),
                    feed,
                    shutdown,
                ),
                name="consumer-pipeline",
            )
            consumer.add_done_callback(_log_consumer_exit)
            log.info("consumer pipeline started")
        elif cfg.run_consumer:
            log.warning(
                "consumer requested but a dependency is down; not starting it",
                broker=js is not None,
                store=clickhouse is not None,
            )
        else:
            log.info("consumer disabled (RUN_CONSUMER=false): ingest API only")

        try:
            yield
        finally:
            # Ordering is the graceful-shutdown contract: uvicorn has already
            # stopped accepting and drained in-flight requests by the time this
            # runs, so ingest is closed. Now let the pipeline flush what it holds
            # before the connections go away.
            shutdown.set()
            if consumer is not None:
                try:
                    # Bounded: a drain that hangs must not hold the process open
                    # forever. `wait_for` cancels the task when the budget runs
                    # out, which is the honest failure — better a lost partial
                    # window than a pod that never terminates.
                    await asyncio.wait_for(consumer, timeout=10)
                except TimeoutError:
                    log.warning("consumer did not drain in time; cancelled")
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    # The consumer died earlier (a vertical's NotImplementedError
                    # while you build it, say). Report it, but never let it turn
                    # a clean shutdown into a failed one.
                    log.warning("consumer task ended with an error", error=str(exc))
            if clickhouse is not None:
                await clickhouse.close()
            if nats_client is not None:
                # Drain (not close) so anything still in flight is flushed — but
                # bounded, because drain waits on every live subscription and the
                # default budget is longer than a container gets before SIGKILL.
                try:
                    await asyncio.wait_for(nats_client.drain(), timeout=5)
                except Exception as exc:  # noqa: BLE001
                    log.warning("broker drain did not finish; closing", error=str(exc))
                    await nats_client.close()
            log.info("shutdown complete")

    app = FastAPI(
        title="metrics-pipeline",
        summary="Line-protocol ingest, windowed rollups, and a live SSE feed (project 05).",
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
    log.info(
        "starting",
        addr=f"0.0.0.0:{cfg.port}",
        hint="POST /ingest to send metrics, GET /stream for the live feed",
    )
    # TODO(protocols, optional): a UDP listener for fire-and-forget StatsD-style
    # senders. When you add it, use `loop.create_datagram_endpoint` with a
    # `DatagramProtocol` feeding a **bounded** `asyncio.Queue` — never a raw
    # socket with `loop.sock_recvfrom`, which works under pytest's stdlib loop
    # and raises NotImplementedError under the uvloop this process actually runs
    # on. The queue bound is the ingest backpressure, and dropping there is a
    # deliberate choice you should count.
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
