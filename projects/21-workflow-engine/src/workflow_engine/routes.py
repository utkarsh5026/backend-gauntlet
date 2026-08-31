"""The admin HTTP surface that rides alongside the gRPC server.

A gRPC service still needs a plain HTTP port, for two reasons that have nothing
to do with each other:

* **Prometheus scrapes HTTP.** There is no gRPC scrape protocol.
* **`/healthz` is what container orchestration understands** without a
  `grpc_health_probe` binary in the image.

Note this is *in addition to* `grpc.health.v1`, which `main` also registers. The
gRPC health service is what a gRPC *client* load balancer checks; `/healthz` is
what Docker and Kubernetes check. Both exist on purpose.

These are Starlette routes rather than a FastAPI app: there is no request body to
validate and no schema to publish, so FastAPI's machinery would be weight with
nothing to carry.

Note that importing `metrics` is not incidental. `prometheus_client` collectors
register themselves into the default registry *at import time*, and
`common_telemetry.metrics_routes()` renders that registry — so a metrics module
nobody imports is a metrics module that does not exist. Importing it here, in the
one module whose job is serving `/metrics`, is what guarantees the series are
there to be scraped before any code path has touched them.
"""

from __future__ import annotations

import common_telemetry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import metrics
from .state import AppState

__all__ = ["create_admin_app"]


def create_admin_app(state: AppState) -> Starlette:
    """Build the `/healthz` + `/metrics` app."""
    # A callback gauge: prometheus_client calls this at scrape time, so the
    # number is read from the live cache instead of being pushed by whichever
    # code path last remembered to. There is no "we forgot to decrement" bug
    # available here, which is the whole reason to prefer it when the value is
    # something you can simply ask for.
    metrics.STICKY_PINS.set_function(lambda: len(state.sticky))

    async def healthz(_request: Request) -> JSONResponse:
        # Liveness, deliberately not readiness: it answers "is this process
        # serving?" without touching Postgres. A health check that queries the
        # database turns one slow query into every engine instance being pulled
        # from rotation at once — and workers whose long-polls are parked on
        # those instances lose their leases for no reason.
        return JSONResponse(
            {
                "status": "ok",
                "timer_service": state.settings.run_timer_service,
                "sticky_pins": len(state.sticky),
            }
        )

    return Starlette(routes=[Route("/healthz", healthz), *common_telemetry.metrics_routes()])
