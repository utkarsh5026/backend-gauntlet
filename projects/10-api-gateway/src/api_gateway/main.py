"""API gateway / L7 reverse proxy — entrypoint and wiring.

The plumbing is wired for you: config, the pooled upstream client, the route
table, the FastAPI app, the health-checker task, `/metrics`, and graceful
shutdown. The learning lives in the modules marked `TODO(Vx)`: the streaming
forwarding core (V1, `proxy.py`), the routing engine (V2, `router.py`), the load
balancer (V3, `balancer.py`), and health checking + circuit breaking (V4,
`health.py`). See SPEC.md.

Scaffold state: this starts and serves. `GET /healthz`, `GET /metrics` and
`GET /admin/routes` work — the last one already shows the table your config
compiled to. The first request that must be *proxied* raises
`NotImplementedError` naming the vertical it needs, and that message is your
worklist.

## Run the demo

```bash
make setup && make up      # three whoami backends on :9010-:9012
make run                   # the gateway on :8080, proxying to that pool
curl localhost:8080/admin/routes
```

## Where graceful shutdown actually happens

The `finally` half of the lifespan. By the time it runs, uvicorn has stopped
accepting new connections and given in-flight requests until
`timeout_graceful_shutdown` to finish, so what is left is to stop the background
health checker and close the upstream client. **The order matters**: cancel the
checker first, then close the client. Closing a client out from under a probe
that is mid-flight turns a clean shutdown into a stack trace, and — worse for a
proxy — `aclose()` tears down the connection pool, which is what in-flight
*proxied* requests are streaming through. Draining before closing is the whole
"no truncated responses" criterion in the SPEC's Protocols checklist.

## A known ceiling, on purpose

The SPEC asks for HTTP/2 to clients. Uvicorn speaks HTTP/1.1 only — there is no
flag for this, h2 is simply not implemented — so reaching that criterion means
fronting this with something that terminates h2, or serving it under Hypercorn.
Do not quietly scale the criterion down: find where the wall is, and record what
it cost and why in `docs/10-benchmarks.md`. Upstream is a different story —
httpx speaks h2 with the `http2` extra, so the gateway->backend leg is reachable
from here.
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
from .errors import install_error_handlers
from .health import HealthChecker
from .router import Router
from .routes import proxy_router, router
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main", "upstream_client"]

SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for the health-checker task to stop before giving up on it.

Short on purpose: its work is a sleep and a set of probes, all individually
bounded, so anything slower than this is stuck rather than busy."""

GRACEFUL_SHUTDOWN_SECONDS = 15
"""How long uvicorn lets in-flight requests finish after SIGTERM.

Longer than most services would use, because a request in flight here can be a
large download that is streaming perfectly well and simply is not finished. Too
short truncates it, which is exactly the failure the Protocols checklist forbids;
too long and a deploy hangs behind one slow client. It should comfortably exceed
`REQUEST_TIMEOUT_MS` so that the deadline, not the shutdown, is what bounds a
request."""

MAX_UPSTREAM_CONNECTIONS = 200
MAX_KEEPALIVE_CONNECTIONS = 100
"""The upstream pool's bounds — the "bounded pool sized on purpose" checklist item.

These two are the gateway's real concurrency limit, whatever else you configure:
once `max_connections` is reached, further requests wait on the pool, and a wait
with no bound is an unbounded queue with extra steps. They want tuning *together*
with whatever concurrency limit you add for load-shedding, and the reasoning
belongs in `docs/10-design.md`. `max_keepalive_connections` below `max_connections`
is deliberate: a burst may open more than the steady state keeps warm."""


def upstream_client(cfg: Settings) -> httpx.AsyncClient:
    """Build the pooled client used for every upstream request (V1).

    One of these exists per process and lives as long as the process does. That is
    not a detail — the connection pool *is* this object, so its lifetime is
    exactly the keep-alive reuse V1 is graded on. A client built per request pays
    a TCP (and upstream TLS) handshake every time while looking perfectly correct.

    Three settings worth understanding rather than copying:

    * `follow_redirects=False`. A proxy relays a `302`; it does not chase it. A
      client that follows redirects would turn one client request into two
      upstream ones and hand the client a response from a URL it never asked for.
    * `timeout` splits connect from read. `connect` bounds reaching a dead
      backend; `read` bounds the *gap between chunks*, not the whole body, so a
      legitimately slow 2 GiB download is not killed for being large while a
      backend that stalls mid-stream still is.
    * `limits` bounds the pool — see `MAX_UPSTREAM_CONNECTIONS`.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.request_timeout,
            write=cfg.request_timeout,
            pool=cfg.connect_timeout,
        ),
        limits=httpx.Limits(
            max_connections=MAX_UPSTREAM_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
        ),
        follow_redirects=False,
        # TODO(mTLS): when UPSTREAM_TLS_CA / UPSTREAM_CLIENT_CERT are set, pass
        # `verify=tls.upstream_context(...)` here for a mutually-authenticated
        # path to the backends. httpx takes an `ssl.SSLContext` directly.
    )


async def _run_health_checker(checker: HealthChecker) -> None:
    """Run the active health checker, tolerating the fact that it isn't built yet.

    Without this wrapper the scaffold would spawn a task that dies instantly with
    an unretrieved `NotImplementedError` — an alarming traceback for a state that
    is entirely expected. Once V4 exists this wrapper becomes a no-op and the
    `except` stops being reachable; deleting it then is correct.
    """
    try:
        await checker.run()
    except NotImplementedError as exc:
        logger.warning("active health checking is still a todo", detail=str(exc))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can stand up several
    independent gateways in one process — different route tables, different pools,
    different policies — which is what testing a router or a balancer honestly
    requires.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        client = upstream_client(cfg)
        gateway_router = Router.build(
            cfg.gateway_config(),
            failure_threshold=cfg.circuit_failure_threshold,
            open_cooldown=cfg.circuit_open_cooldown,
        )
        app.state.app_state = AppState(settings=cfg, client=client, router=gateway_router)

        # Held in a local for the whole lifespan: a bare `create_task` result that
        # nobody keeps can be garbage-collected mid-flight. Here that would mean
        # active health checking silently stopping while `/healthz` still answers
        # `ok` — a dead backend would keep taking traffic and nothing would say so.
        checker = HealthChecker(gateway_router, client, cfg.health_probe_interval)
        health_task = asyncio.create_task(_run_health_checker(checker), name="health-checker")

        logger.info(
            "gateway initialized",
            routes=len(gateway_router.routes),
            backends=len(gateway_router.backends()),
            request_timeout_ms=cfg.request_timeout_ms,
            probe_interval_ms=cfg.health_probe_ms,
        )
        try:
            yield
        finally:
            # Stop probing before tearing down the pool those probes run on —
            # see the module docstring on shutdown ordering.
            health_task.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_BUDGET):
                    await health_task
            except (TimeoutError, asyncio.CancelledError):
                pass

            await client.aclose()
            logger.info("shutdown complete")

    app = FastAPI(
        title="api-gateway",
        summary="An L7 API gateway / reverse proxy built from the byte path up (project 10).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while proxying carries the request id, and
    # the id is echoed back to the client — which is what lets you take an id off
    # a failed response and find the matched route and chosen backend in the log.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)

    # Registration order is load-bearing: the gateway's own endpoints and the
    # metrics routes must both come before the catch-all, or `/metrics` gets
    # proxied to a backend. See routes.py.
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    app.include_router(proxy_router)
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    logger.info(
        "starting",
        addr=f"0.0.0.0:{cfg.port}",
        hint="GET /admin/routes for the table; every other path is proxied",
    )
    if cfg.tls_enabled:
        # TODO(mTLS): pass ssl_certfile / ssl_keyfile — and, when TLS_CLIENT_CA is
        # set, ssl_ca_certs plus ssl_cert_reqs=ssl.CERT_REQUIRED for edge mTLS —
        # into uvicorn.run below. See tls.py for what the context has to assert.
        logger.warning("TLS_CERT/TLS_KEY are set but TLS termination is still a todo (tls.py)")

    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Worth knowing
        # that this is *not* the loop pytest runs on, which is why the SPEC's
        # Definition of done asks you to boot the container: some bugs only exist
        # under one of the two loops.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request;
        # uvicorn's own access log would just double the I/O on the hot path.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
