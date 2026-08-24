"""Lambda compute plane — entrypoint and wiring.

The plumbing (config, the registry, both ASGI apps, telemetry, the background
tasks, graceful shutdown) is wired for you. The learning lives in the modules
marked `TODO(Vx)`: the Runtime API broker (V1, `runtime_api.py`), execution
environments (V2, `environments.py`), the sandbox (V3, `sandbox.py`), the
concurrency governor (V4, `concurrency.py`), async invocation (V5,
`async_invoke.py`) and event source mappings (V6, `event_source.py`). See SPEC.md.

**Two listeners, on purpose.** The control plane (`PORT`) is where callers register
and invoke functions. The Runtime API (`RUNTIME_API_PORT`) is where a sandboxed
runtime long-polls for work. Real Lambda separates them because the sandbox must be
able to reach the second and not the first — keeping them apart here is what makes
V3's "the sandbox cannot reach the control plane" a boundary you can test rather
than a sentence in a design doc. Both apps share one `AppState`, which is how an
invocation submitted on one port reaches a runtime polling on the other.

Scaffold state: this starts and serves. `GET /healthz`, the function registry,
event-source-mapping registration and `GET /metrics` work; the first real
invocation raises a `NotImplementedError` — that is your worklist.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncGenerator, Awaitable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from .async_invoke import AsyncInvocationQueue
from .concurrency import ConcurrencyGovernor
from .config import Settings
from .environments import EnvironmentPool
from .errors import install_error_handlers
from .routes import public_router
from .runtime_api import InvocationBroker, runtime_router
from .state import AppState, FunctionRegistry

log = structlog.get_logger(__name__)

# How often the reaper sweeps for idle environments. Not a setting: it is a
# sampling interval, not a policy — the policy is ENVIRONMENT_IDLE_TTL_SECONDS.
REAP_INTERVAL_SECONDS = 5.0


def build_state(settings: Settings) -> AppState:
    """Assemble the runtime. One instance, shared by both apps."""
    governor = ConcurrencyGovernor(settings)
    return AppState(
        settings=settings,
        registry=FunctionRegistry(settings, governor),
        governor=governor,
        pool=EnvironmentPool(settings),
        broker=InvocationBroker(),
        async_queue=AsyncInvocationQueue(settings),
    )


async def _scaffold_guard(label: str, coro: Coroutine[Any, Any, Any]) -> None:
    """Await a background coroutine, tolerating the ones that aren't built yet.

    **Scaffold-only.** Every background task here calls into a vertical that still
    raises, and an unhandled `NotImplementedError` in a task would either take the
    node down or vanish into a "task exception was never retrieved" warning. This
    turns it into one honest log line instead, so `make run` boots and stays up
    while you work through the SPEC.

    Once a vertical is built its task runs for real and this becomes a no-op. It
    deliberately does **not** swallow anything else — a real bug in your code must
    still crash loudly.
    """
    try:
        await coro
    except NotImplementedError as exc:
        log.info("background task not implemented yet", task=label, detail=str(exc))
    except asyncio.CancelledError:
        raise


async def _reap_loop(state: AppState) -> None:
    """Sweep idle environments on a timer (V2's reaper)."""
    while True:
        await asyncio.sleep(REAP_INTERVAL_SECONDS)
        reaped = await state.pool.reap_idle()
        if reaped:
            log.info("environments reaped", count=reaped, fleet=state.pool.stats())


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the control-plane ASGI app.

    A factory rather than a module-level `app` so tests can construct an
    independent node without touching the environment. Pass `state` to share one
    runtime with a Runtime API app; omit it and the lifespan builds its own.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app_state = state if state is not None else build_state(cfg)
        app.state.app_state = app_state

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(_scaffold_guard("environment-reaper", _reap_loop(app_state))),
            asyncio.create_task(
                _scaffold_guard("async-invoke-worker", app_state.async_queue.run_worker())
            ),
        ]
        # TODO(V6): start one `EventSourcePoller.run()` task per registered
        # mapping here, and stop them in the shutdown below. They live in the
        # lifespan rather than in the route that creates a mapping so that
        # shutdown has exactly one place to drain them.

        log.info(
            "node ready",
            control_port=cfg.port,
            runtime_api=cfg.runtime_api_address,
            account_concurrency_limit=cfg.account_concurrency_limit,
            max_environments=cfg.max_environments,
        )
        try:
            yield
        finally:
            # Order matters and is graded by the SPEC: stop accepting work, drain
            # what is in flight, THEN tear down the environments running it.
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # TODO(V5/V2): drain in-flight invocations up to a deadline before the
            # pool is destroyed, and report what was abandoned. Tearing the
            # environments down first would abandon acknowledged work silently.
            await _scaffold_guard("environment-pool-shutdown", app_state.pool.shutdown())
            log.info("shutdown complete")

    app = FastAPI(
        title="lambda-compute",
        summary="A serverless compute plane built from the Runtime API up (project 24).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(public_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def create_runtime_app(state: AppState) -> FastAPI:
    """Build the Runtime API app — the surface a sandboxed runtime long-polls.

    Deliberately minimal: no metrics endpoint, no control-plane routes. The less
    this listener can do, the less a hostile function gets by reaching it.
    """
    app = FastAPI(
        title="lambda-runtime-api",
        summary="The Runtime API a sandboxed execution environment polls (project 24).",
    )
    app.state.app_state = state
    # `runtime_api` resolves the broker off the app it is mounted on, so a test can
    # drive this app standalone without booting the control plane.
    app.state.broker = state.broker
    install_error_handlers(app)
    app.include_router(runtime_router)
    return app


def create_stack(settings: Settings) -> tuple[FastAPI, FastAPI]:
    """Both apps over one shared state. The shape `main` and the tests both use."""
    state = build_state(settings)
    return create_app(settings, state=state), create_runtime_app(state)


class _SharedSignalServer(uvicorn.Server):
    """A uvicorn server that does not install its own signal handlers.

    Two servers in one process would otherwise fight over them: each installs its
    own during `serve()`, the second wins, and a SIGTERM stops only that one while
    the other keeps the process alive forever. `_serve` installs one handler that
    stops both.
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]  # uvicorn types this loosely
        yield


async def _serve(cfg: Settings, app: FastAPI, runtime_app: FastAPI) -> None:
    """Run both listeners until a signal stops them."""

    def _config(target: FastAPI, host: str, port: int) -> uvicorn.Config:
        return uvicorn.Config(
            target,
            host=host,
            port=port,
            # "auto" picks uvloop, which uvicorn[standard] installs. Uvicorn's own
            # access log is off because RequestIdMiddleware already emits one
            # structured line per request.
            loop="auto",
            access_log=False,
            log_config=None,
        )

    control = _SharedSignalServer(_config(app, "0.0.0.0", cfg.port))
    # Loopback only: the Runtime API is reachable from the sandboxes on this node
    # and from nowhere else.
    runtime = _SharedSignalServer(_config(runtime_app, cfg.runtime_api_host, cfg.runtime_api_port))

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        log.info("signal received, draining")
        control.should_exit = True
        runtime.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    servers: list[Awaitable[None]] = [control.serve(), runtime.serve()]
    try:
        await asyncio.gather(*servers)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    app, runtime_app = create_stack(cfg)
    log.info(
        "starting",
        control_addr=f"0.0.0.0:{cfg.port}",
        runtime_api_addr=cfg.runtime_api_address,
        hint="POST /2015-03-31/functions to register one, then .../invocations",
    )
    asyncio.run(_serve(cfg, app, runtime_app))


if __name__ == "__main__":
    main()
