"""The admin HTTP surface that rides alongside the gRPC server.

A gRPC service still needs a plain HTTP port, for two reasons that have nothing
to do with each other:

  * **Prometheus scrapes HTTP.** There is no gRPC scrape protocol.
  * **`/healthz` is what container orchestration understands** without a
    `grpc_health_probe` binary in the image.

Note that this is *in addition to* `grpc.health.v1`, which `main` also registers.
The gRPC health service is what a gRPC *client* load-balancer checks; `/healthz`
is what Docker and Kubernetes check. Both exist on purpose.

These are Starlette routes rather than a FastAPI app: there is no request body to
validate and no schema to publish, so FastAPI's machinery would be weight with
nothing to carry.
"""

from __future__ import annotations

import common_telemetry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .state import AppState

__all__ = ["create_admin_app"]


def create_admin_app(state: AppState) -> Starlette:
    """Build the `/healthz` + `/metrics` app."""

    async def healthz(_request: Request) -> JSONResponse:
        # Liveness, deliberately not readiness: it answers "is this process
        # serving?" without touching Redis. A health check that pings the
        # backend turns one Redis blip into every instance being pulled from
        # rotation at once -- which, for a limiter that can fail open, is a much
        # worse outcome than serving degraded.
        return JSONResponse(
            {
                "status": "ok",
                "algorithm": str(state.settings.algorithm),
                "fail_open": state.settings.fail_open,
            }
        )

    return Starlette(routes=[Route("/healthz", healthz), *common_telemetry.metrics_routes()])
