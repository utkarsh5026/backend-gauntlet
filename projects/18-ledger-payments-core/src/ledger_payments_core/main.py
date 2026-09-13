"""Ledger / payments core (Stripe-lite) — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the Postgres pool and the Redis
client, the FastAPI app and `/metrics`, the webhook dispatcher, and graceful shutdown.
The learning lives in the modules marked `TODO(Vx)`:

* V1 `ledger.py`      — the double-entry posting engine
* V2 `isolation.py`   — the transfer that stays correct under concurrency
* V3 `idempotency.py` — idempotency keys over a Redis cache and a Postgres record
* V4 `webhooks.py`    — signed delivery through a transactional outbox

Scaffold state: this starts and serves once `make up` has Postgres and Redis running.
`/healthz` and `/metrics` answer; every money route answers `501` naming the todo that
blocks it. `RUN_DISPATCHER=true` starts the dispatcher, which stops on its first round
and logs the V4 todo that stopped it — that is your worklist.

## Shutdown order

`uvicorn.run` installs the SIGTERM handler. On SIGTERM uvicorn stops accepting
connections and lets in-flight requests — in-flight *transfers* — finish, for up to
`GRACEFUL_SHUTDOWN_SECONDS`. *Then* the lifespan's `finally` runs, which:

1. sets `state.shutdown`, so the dispatcher starts no new round;
2. waits up to `DISPATCHER_DRAIN_SECONDS` for the round it's on to settle;
3. cancels it if it hasn't, then closes the HTTP client, Redis, and the pool — in that
   order, so nothing still running loses a connection it's using.

A transfer cut off at the grace period is safe *because of V1*: its transaction rolls
back whole, and a client that retries with its `Idempotency-Key` still gets exactly one
posting (V3). A delivery cut off mid-round is safe *because of V4*: the event is still
in the outbox. Graceful shutdown is an optimisation on top of those guarantees, never a
substitute for them.

## What CPython costs you here, stated up front

The Double Spend asks for ≥ 2,000 transfers/sec at p99 ≤ 25 ms. Those numbers are not
scaled down for Python. Each transfer is a few Postgres round-trips plus request
parsing, pydantic validation and JSON encoding — interpreter CPU, one event loop and
one GIL per process. If one process can't get there, how many does it take, and does
the connection-pool arithmetic still hold at that count? That gap and its cause are
the finding, recorded in `docs/18-benchmarks.md`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import asyncpg
import common_telemetry
import httpx
import structlog
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis

from . import db, metrics, webhooks
from .config import Settings
from .errors import install_error_handlers
from .idempotency import IdempotencyStore
from .isolation import TransferPolicy
from .ledger import Ledger
from .routes import router
from .state import AppState, task_failure

logger = structlog.get_logger(__name__)

__all__ = [
    "build_state",
    "create_app",
    "create_redis",
    "main",
    "start_dispatcher",
    "stop_background",
    "webhook_config",
]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn lets in-flight HTTP requests finish after SIGTERM. A transfer is a
few round-trips — unless it's retrying against a hot account, which is exactly when
you'll learn whether this number was enough."""

DISPATCHER_DRAIN_SECONDS = 15.0
"""How long shutdown waits for the dispatcher's current round to settle.

Chosen so both budgets together fit k8s' default 30 s grace period, after which the
kubelet sends SIGKILL and nothing in the `finally` runs. (`docker stop` defaults to
10 s — pass `-t 30` to watch a full drain.)"""

CANCEL_SECONDS = 3.0
"""How long to wait for a cancelled background task to actually stop."""


def create_redis(settings: Settings) -> Redis:
    """The idempotency cache's client. Constructing it does not connect: redis-py dials
    lazily, on the first command.

    That laziness is a feature here. The cache is not the record (V3), so a ledger that
    refused to boot while Redis is down would be treating an optimisation as a
    dependency. It also lets the tests build the whole app with no Redis running.
    """
    # redis-py types `from_url`'s **kwargs loosely, so pyright cannot see the keyword
    # arguments below; the returned client is typed.
    client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )
    return client


def build_state(
    settings: Settings, *, pool: asyncpg.Pool[asyncpg.Record], redis: Redis
) -> AppState:
    """Assemble the ledger over an already-constructed pool and Redis client. No I/O.

    Split out from the lifespan so tests can build the whole app over a pool that never
    connects — asyncpg's `create_pool()` returns an unconnected pool until it is
    awaited — and a Redis client that never dials. The scaffold suite runs in CI that
    way, with neither service.
    """
    return AppState(
        settings=settings,
        pool=pool,
        redis=redis,
        ledger=Ledger(pool),
        idempotency=IdempotencyStore(pool, redis, ttl_secs=settings.idempotency_ttl_secs),
        policy=TransferPolicy(
            max_retries=settings.max_serialization_retries,
            max_amount=settings.max_transfer_minor,
            webhook_endpoint=settings.webhook_endpoint_url,
        ),
    )


def webhook_config(settings: Settings) -> webhooks.WebhookConfig:
    """The dispatcher's tuning, lifted out of `Settings` with the secret still masked."""
    return webhooks.WebhookConfig(
        signing_secret=settings.webhook_signing_secret,
        endpoint_url=settings.webhook_endpoint_url,
        max_attempts=settings.webhook_max_attempts,
        dispatch_interval_secs=settings.webhook_dispatch_interval_secs,
        dispatch_batch=settings.webhook_dispatch_batch,
        timeout_secs=settings.webhook_timeout_secs,
    )


def _log_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead background task the moment it dies.

    A task that raises does so *silently*: nothing is printed until someone awaits it
    or the garbage collector complains. For the dispatcher that silence looks exactly
    like a quiet outbox — events just stop going out while `/healthz` says `ok`. This
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


def start_dispatcher(state: AppState, http: httpx.AsyncClient) -> None:
    """Start the webhook dispatcher as a task on this loop."""
    task = asyncio.create_task(
        webhooks.dispatch_loop(state.pool, http, webhook_config(state.settings), state.shutdown),
        name="webhook-dispatcher",
    )
    task.add_done_callback(_log_task_exit)
    state.background.append(task)


async def stop_background(state: AppState, *, drain_secs: float = DISPATCHER_DRAIN_SECONDS) -> None:
    """Stop starting rounds, let the current one settle within `drain_secs`, cancel the rest."""
    if not state.background:
        return
    state.shutdown.set()
    _, pending = await asyncio.wait(state.background, timeout=drain_secs)
    if not pending:
        logger.info("background tasks drained")
        return
    logger.warning(
        "drain budget exhausted: cancelling, the outbox still holds what they had claimed",
        cancelled=len(pending),
    )
    for task in pending:
        task.cancel()
    await asyncio.wait(pending, timeout=CANCEL_SECONDS)


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    """Build the ASGI app.

    With no `state`, the lifespan opens the pool and the Redis client itself
    (production). With a `state`, it installs that one and opens nothing — the caller
    owns them. That is how the tests run the real app with no services.
    """
    config = state.settings if state is not None else (settings or Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            if state is None:
                # Never log either URL: both can carry a password.
                pool = await db.create_pool(
                    config.database_url, min_size=1, max_size=config.db_max_connections
                )
                stack.push_async_callback(pool.close)
                logger.info("connected to postgres")
                redis = create_redis(config)
                stack.push_async_callback(redis.aclose)
                app_state = build_state(config, pool=pool, redis=redis)
            else:
                app_state = state
            app.state.app_state = app_state

            if config.run_dispatcher:
                http = httpx.AsyncClient(
                    timeout=config.webhook_timeout_secs,
                    # A redirect is a failed delivery, not an instruction: following it
                    # would POST signed payloads wherever a merchant's endpoint points.
                    follow_redirects=False,
                )
                stack.push_async_callback(http.aclose)
                start_dispatcher(app_state, http)
            else:
                logger.info("webhook dispatcher disabled (RUN_DISPATCHER=false): API only")
            try:
                yield
            finally:
                # Before the exit stack unwinds: a settling round still needs the
                # pool and the HTTP client.
                await stop_background(app_state)
                logger.info("shutdown complete")

    app = FastAPI(
        title="ledger-payments-core",
        summary="Double-entry ledger, safe transfers, idempotency keys, signed webhooks.",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    metrics.init()
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    config = Settings()
    common_telemetry.init(config.log_level)
    logger.info(
        "starting",
        http_addr=f"0.0.0.0:{config.port}",
        run_dispatcher=config.run_dispatcher,
        db_max_connections=config.db_max_connections,
    )
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.port,
        # "auto" picks uvloop, which uvicorn[standard] installs — and which is *not*
        # the loop pytest runs on. That is why the Python checklist asks you to boot
        # the container.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
