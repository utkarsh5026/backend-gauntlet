"""IAM + STS — entrypoint and wiring.

The plumbing (config, the identity store, both ASGI apps, telemetry, the
background tasks, graceful shutdown) is wired for you. The learning lives in the
modules marked `TODO(Vx)`: SigV4 (V1, `sigv4.py`), the policy language (V2,
`policy.py`), the evaluation chain (V3, `evaluation.py`), STS (V4, `sts.py`), the
hot path (V5, `authorizer.py`) and revocation + audit (V6, `audit.py`). See
SPEC.md.

**Two listeners, on purpose.** The AWS-shaped API (`PORT`) is where people create
users and assume roles; every request there must be SigV4-signed. The
authorization endpoint (`AUTHZ_PORT`) is where *other services* — projects 23, 24
and 06 — ask whether a request they received is allowed. Real IAM separates these
planes, and separating them here buys three things the SPEC grades: the hot path
can be benchmarked alone, control-plane writes provably do not share a bottleneck
with decisions, and "the authorizer is reachable only from inside" is a boundary
you can test rather than a sentence in a design doc.

Both apps share one `AppState`. That is what makes the propagation window (V5) a
real measurement: a policy written on :9025 has to become visible to a decision
served on :9026, and how fast that happens is a number rather than an artifact of
the plumbing.

Scaffold state: this starts and serves. `GET /healthz` on both ports and
`GET /metrics` work, and an unsigned request is correctly refused. The first
*signed* request raises a `NotImplementedError` from V1 — that is your worklist,
and it is the front door on purpose.
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

from .audit import AuditLog, PolicySimulator, RevocationRegistry
from .authorizer import Authorizer, DecisionCache, PolicyCompiler
from .config import Settings
from .errors import install_error_handlers
from .evaluation import PolicyEvaluator
from .policy import ConditionEvaluator
from .routes import authz_router, public_router
from .sigv4 import SigV4Verifier
from .state import AppState, IdentityStore
from .sts import SecurityTokenService, SessionTokenCodec

log = structlog.get_logger(__name__)


def build_state(settings: Settings) -> AppState:
    """Assemble the runtime. One instance, shared by both apps.

    Read the order of construction as the dependency graph of the SPEC: the
    condition evaluator feeds the policy evaluator, which feeds both the
    authorizer (the cached hot path) and the simulator (the uncached truth). That
    the last two share an evaluator is not an optimization — it is what makes
    V6's parity criterion achievable at all.
    """
    store = IdentityStore(settings)
    store.seed_bootstrap()

    conditions = ConditionEvaluator(settings)
    evaluator = PolicyEvaluator(conditions)
    return AppState(
        settings=settings,
        store=store,
        verifier=SigV4Verifier(settings),
        evaluator=evaluator,
        authorizer=Authorizer(
            settings,
            evaluator,
            PolicyCompiler(settings),
            DecisionCache(settings),
        ),
        sts=SecurityTokenService(settings, SessionTokenCodec(settings)),
        audit=AuditLog(settings),
        revocations=RevocationRegistry(settings),
        simulator=PolicySimulator(evaluator),
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


async def _audit_flush_loop(state: AppState) -> None:
    """Drain the audit queue on a timer (V6's writer)."""
    interval = state.settings.audit_flush_interval_seconds
    while True:
        await asyncio.sleep(interval)
        written = await state.audit.flush()
        if written:
            log.debug("audit flushed", records=written, shed=state.audit.shed_total)


async def _session_reap_loop(state: AppState) -> None:
    """Sweep expired sessions out of the table (V4's housekeeping).

    Bookkeeping, not enforcement: expiry is enforced by the token's own claim.
    A session lingering here past its expiry is a memory leak, not a security
    hole — and one that vanishes from here early is not thereby revoked.
    """
    interval = state.settings.session_reap_interval_seconds
    while True:
        await asyncio.sleep(interval)
        reaped = await state.sts.reap_expired()
        if reaped:
            log.info("sessions reaped", count=reaped, live=state.sts.live_session_count())


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the AWS-shaped API app (the management/STS plane).

    A factory rather than a module-level `app` so tests can construct an
    independent node without touching the environment. Pass `state` to share one
    runtime with the authorizer app; omit it and the lifespan builds its own.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app_state = state if state is not None else build_state(cfg)
        app.state.app_state = app_state

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(_scaffold_guard("audit-flush", _audit_flush_loop(app_state))),
            asyncio.create_task(_scaffold_guard("session-reaper", _session_reap_loop(app_state))),
        ]

        log.info(
            "account ready",
            api_port=cfg.port,
            authz=cfg.authz_address,
            account_id=cfg.account_id,
            region=cfg.aws_region,
            # The one number worth seeing at startup, because it is a security
            # promise: a revoked permission survives at most this long.
            decision_cache_ttl_seconds=cfg.decision_cache_ttl_seconds,
        )
        try:
            yield
        finally:
            # Order matters and is graded by the SPEC: stop the loops, then flush
            # the audit trail. Tearing down before the flush would drop records
            # for decisions that were already served — which is precisely the gap
            # an auditor would find.
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            written = 0
            try:
                written = await app_state.audit.flush()
            except NotImplementedError as exc:
                log.info("final audit flush not implemented yet", detail=str(exc))
            log.info("shutdown complete", audit_records_flushed=written)

    app = FastAPI(
        title="iam-sts",
        summary="Identity, signatures and authorization — IAM + STS (project 25).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(public_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def create_authz_app(state: AppState) -> FastAPI:
    """Build the authorization app — the surface projects 23/24/06 call.

    Deliberately minimal: no management routes, no `AssumeRole`. The less this
    listener can do, the less a service that has been compromised gets by
    reaching it. It keeps `/metrics` because the numbers the boss fight measures
    — decision latency, cache hit ratio — are generated here, and scraping them
    off the other port would mean measuring the wrong process's behaviour.
    """
    app = FastAPI(
        title="iam-authorizer",
        summary="The authorization endpoint other services call (project 25).",
    )
    app.state.app_state = state
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(authz_router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def create_stack(settings: Settings) -> tuple[FastAPI, FastAPI]:
    """Both apps over one shared state. The shape `main` and the tests both use."""
    state = build_state(settings)
    return create_app(settings, state=state), create_authz_app(state)


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


async def _serve(cfg: Settings, app: FastAPI, authz_app: FastAPI) -> None:
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

    api = _SharedSignalServer(_config(app, "0.0.0.0", cfg.port))
    # Loopback only: the authorization endpoint is an internal contract between
    # this service and the projects it protects.
    authz = _SharedSignalServer(_config(authz_app, cfg.authz_host, cfg.authz_port))

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        log.info("signal received, draining")
        api.should_exit = True
        authz.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    servers: list[Awaitable[None]] = [api.serve(), authz.serve()]
    try:
        await asyncio.gather(*servers)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    app, authz_app = create_stack(cfg)
    log.info(
        "starting",
        api_addr=f"0.0.0.0:{cfg.port}",
        authz_addr=cfg.authz_address,
        hint="every request to the API must be SigV4-signed — try `make whoami`",
    )
    asyncio.run(_serve(cfg, app, authz_app))


if __name__ == "__main__":
    main()
