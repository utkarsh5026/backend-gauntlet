"""Managed queue service — entrypoint and wiring.

The plumbing (config, the queue store, the app, telemetry, the deadline loop,
graceful shutdown) is wired for you. The learning lives in the modules marked
`TODO(Vx)`: receipt handles (V1, `inflight.py`), the deadline engine (V2,
`timers.py`), long polling (V3, `polling.py`), FIFO groups (V4, `fifo.py`), the
dedup window (V5, `dedup.py`) and the control plane (V6, `control.py`). See
`SPEC.md`.

**One process, one deadline loop.** Everything that happens "later" in this
service goes through a single engine (V2), started here and stopped on shutdown.
That is a design decision worth seeing at the top level rather than discovering
in a module: four separate sweep loops would each be individually reasonable and
collectively be the reason an idle queue costs a core.

**Shutdown order is graded.** Stop accepting work, release parked long-poll
waiters with an *empty response* rather than a dropped connection, then stop the
deadline loop — and deliberately do **not** touch in-flight leases. A consumer
holding a message when you restart is not an error to clean up; its lease will
expire on its own, which is exactly the behaviour V1 built.

Scaffold state: this starts and serves. `GET /healthz` and `GET /metrics` work,
and a malformed `X-Amz-Target` is refused correctly. The first real action raises
`NotImplementedError` — that is your worklist, and it is the front door on
purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Settings
from .control import ControlPlane
from .dedup import DedupWindow
from .errors import install_error_handlers
from .fifo import GroupIndex
from .inflight import InflightTable, ReceiptHandleCodec
from .polling import WaitSet
from .routes import public_router
from .state import AppState, QueueStore
from .timers import DeadlineEngine, DeadlineKind

log = structlog.get_logger(__name__)


def build_state(settings: Settings) -> AppState:
    """Assemble the runtime. One instance, shared by every request.

    Read the order of construction as the dependency graph of the SPEC: the codec
    feeds the in-flight table, the deadline engine drives both it and the dedup
    window, and the wait set and group index sit in front of receives. That the
    control plane is built last is not an accident — it configures all of them.
    """
    store = QueueStore(settings)
    codec = ReceiptHandleCodec(settings)
    deadlines = DeadlineEngine(settings)
    inflight = InflightTable(settings, codec)
    dedup = DedupWindow(settings)

    # Every "later" in the service, wired to one engine. The lambdas are thin on
    # purpose: the generation check that makes each of these races decidable
    # belongs in the vertical, not here.
    deadlines.register(
        DeadlineKind.VISIBILITY,
        lambda d, now: inflight.expire_visibility(d.key, d.generation, now),
    )
    deadlines.register(DeadlineKind.DEDUP, lambda d, now: dedup.expire(d.queue_name, d.key, now))

    return AppState(
        settings=settings,
        store=store,
        codec=codec,
        inflight=inflight,
        deadlines=deadlines,
        waiters=WaitSet(settings),
        groups=GroupIndex(settings),
        dedup=dedup,
        control=ControlPlane(settings),
    )


async def _scaffold_guard(label: str, coro: Coroutine[Any, Any, Any]) -> None:
    """Await a background coroutine, tolerating the ones that aren't built yet.

    **Scaffold-only.** The deadline loop calls into a vertical that still raises,
    and an unhandled `NotImplementedError` in a task would either take the node
    down or vanish into a "task exception was never retrieved" warning. This turns
    it into one honest log line instead, so `make run` boots and stays up while
    you work through the SPEC.

    Once V2 is built the loop runs for real and this becomes a no-op. It
    deliberately does **not** swallow anything else — a real bug must still crash
    loudly.
    """
    try:
        await coro
    except NotImplementedError as exc:
        log.info("background task not implemented yet", task=label, detail=str(exc))
    except asyncio.CancelledError:
        raise


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent node without touching the environment.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app_state = state if state is not None else build_state(cfg)
        app.state.app_state = app_state

        deadline_task = asyncio.create_task(
            _scaffold_guard("deadline-engine", app_state.deadlines.run())
        )

        log.info(
            "broker ready",
            port=cfg.port,
            account_id=cfg.account_id,
            region=cfg.aws_region,
            # The two numbers worth seeing at startup, because both are promises:
            # how long a crashed consumer's message stays invisible, and how long
            # a producer's retry keeps deduplicating.
            default_visibility_timeout_seconds=cfg.default_visibility_timeout_seconds,
            dedup_window_seconds=cfg.dedup_window_seconds,
        )
        try:
            yield
        finally:
            # Order matters and is graded by the SPEC. Waiters first: they are
            # holding connections, and they get a polite empty response rather
            # than a reset. In-flight leases are deliberately left alone — they
            # expire on their own, which is the whole point of V1.
            released = 0
            try:
                released = app_state.waiters.release_all()
            except NotImplementedError as exc:
                log.info("waiter release not implemented yet", detail=str(exc))

            deadline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await deadline_task

            log.info(
                "shutdown complete",
                waiters_released=released,
                queues=len(app_state.store.queues),
                messages=app_state.store.total_messages(),
            )

    app = FastAPI(
        title="sqs-queue",
        summary="A managed queue service — receipt handles, FIFO groups, dedup and long polling.",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(public_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


async def _serve(cfg: Settings, app: FastAPI) -> None:
    """Run the listener until a signal stops it.

    Uvicorn installs its own SIGINT/SIGTERM handlers and drives a graceful
    shutdown from them — accept nothing new, finish what is open, then run the
    lifespan's teardown. That is exactly the sequence the SPEC's graceful-shutdown
    criterion wants, so this deliberately does *not* add handlers of its own:
    two owners of the same signal is how one of them silently stops working.
    """
    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
        # access log is off because RequestIdMiddleware already emits one
        # structured line per request — and at the boss fight's message rates, two
        # log lines per request is itself a measurable cost.
        loop="auto",
        access_log=False,
        log_config=None,
        # Long polling holds a connection for up to 20 seconds by design. The
        # default keep-alive timeout is shorter than that, which would close
        # connections out from under waiters that are behaving correctly — and
        # would look, from the client side, exactly like the service dropping
        # requests under load.
        timeout_keep_alive=int(cfg.max_receive_wait_time_seconds) + 10,
        # Give parked waiters time to be released politely instead of reset.
        timeout_graceful_shutdown=10,
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    log.info(
        "starting",
        addr=f"{cfg.host}:{cfg.port}",
        hint="try `make smoke`, then `make create-queue`",
    )
    asyncio.run(_serve(cfg, create_app(cfg)))


if __name__ == "__main__":
    main()
