"""HTTP surface: the gateway's own endpoints, and the catch-all that proxies
everything else.

Only three paths belong to the gateway — `/healthz`, `/admin/routes` and
`/metrics` (mounted in `main.py`). **Every other path is a proxy target**, which
makes this router shaped unlike any other project's in the repo: the last route
registered is a catch-all that matches anything, and its job is
route match (V2) -> backend pick (V3) -> forward (V1).

Scaffold behavior: `/healthz`, `/admin/routes` and `/metrics` answer immediately.
The first request that must actually be *proxied* raises `NotImplementedError`
naming the vertical it needs. That message is your worklist.

## The ordering rule this file depends on

Starlette matches routes **in registration order** and stops at the first hit. A
catch-all registered before `/metrics` swallows `/metrics`, and you get a
Prometheus scrape proxied to a random backend instead of an error — which is a
memorable afternoon. So the gateway's own endpoints, *including* the metrics
routes mounted in `main.py`, must all be registered before `proxy_router`. That is
why the catch-all lives in a second router: the ordering requirement is visible in
`create_app` instead of hiding in the order of two decorators.

The same rule is why these three paths are effectively reserved names — a backend
of yours that genuinely serves `/metrics` cannot be reached through this gateway
without a prefix. Real gateways solve that by binding admin to a *separate port*,
which is a legitimate thing to do here and worth a line in `docs/10-design.md`
either way.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from . import proxy
from .errors import NoHealthyBackend, NoRoute
from .state import AppState, get_state

__all__ = ["PROXIED_METHODS", "proxy_router", "router"]

StateDep = Annotated[AppState, Depends(get_state)]

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
"""The methods the catch-all accepts.

Starlette needs them listed explicitly. `CONNECT` is absent because it is a
tunnelling method a forward proxy implements, not a reverse one, and `TRACE`
because echoing a request back through a gateway that adds provenance headers is
a well-known way to leak them."""

router = APIRouter()
"""The gateway's own endpoints. Must be registered before `proxy_router`."""

proxy_router = APIRouter()
"""The catch-all. Registered last — see the module docstring."""


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only.

    Deliberately *not* a readiness check, and this is a sharper distinction for a
    gateway than for most services: this answers `ok` even when every upstream is
    down, because the gateway is up and correctly reporting 503s. Wiring an
    orchestrator to restart on backend failure would take a partial outage and
    turn it into a total one — restarting the proxy does not fix the backends, it
    just removes the thing that was routing around them.
    """
    return {"status": "ok"}


@router.get("/admin/routes")
async def list_routes(state: StateDep) -> dict[str, Any]:
    """The loaded route table, for eyeballing config.

    Wired and safe to call before any vertical exists — it reads the table that
    `Router.build` compiled at startup, so it is the fastest way to confirm your
    `CONFIG_PATH` was actually picked up and parsed the way you meant.

    TODO(security): this enumerates internal topology — route names now, and it is
    tempting to add backend addresses while debugging. Decide whether `/admin` is
    bound to a separate port, restricted by network policy, or authenticated, and
    record it in `docs/10-design.md`. A gateway that publishes its own upstream
    map to the internet has done an attacker's reconnaissance for them.
    """
    return {"routes": state.router.route_names()}


@proxy_router.api_route(
    "/{full_path:path}",
    methods=PROXIED_METHODS,
    include_in_schema=False,
)
async def proxy_handler(full_path: str, request: Request, state: StateDep) -> Response:
    """Catch-all: proxy the request to whichever backend the route and balancer
    select.

    `full_path` is declared only because Starlette requires the path parameter to
    exist for `{full_path:path}` to match — it is intentionally unused. The proxy
    reads the target from `request.url` and `request.scope` instead, because a
    path parameter arrives percent-**decoded** and re-encoding it is lossy. See
    `proxy.forward`.

    TODO(security): before proxying, enforce the edge protections the SPEC lists —
    the body-size cap, edge auth (reject an unauthenticated request *without*
    touching an upstream, which is half the value of doing it here), and stripping
    client-supplied `X-Forwarded-*` and internal auth headers so a caller cannot
    impersonate the proxy to a backend that trusts it.
    """
    del full_path  # see the docstring: declared to make the route match, read from scope

    # V2: which route? -> V3: which backend? -> V1: forward it.
    route = state.router.match_request(
        request.headers.get("host"),
        request.url.path,
        request.method,
    )
    if route is None:
        raise NoRoute()

    backend = route.upstream.balancer.pick()
    if backend is None:
        raise NoHealthyBackend()

    return await proxy.forward(
        state.client,
        backend,
        request,
        deadline=state.settings.request_timeout,
        max_body_bytes=state.settings.max_body_bytes,
    )
