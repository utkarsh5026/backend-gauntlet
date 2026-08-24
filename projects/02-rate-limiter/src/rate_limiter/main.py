"""Distributed rate limiter — entrypoint and wiring.

The plumbing is done for you: config, telemetry, the Redis pool, the gRPC server
with health checking and reflection, the admin HTTP port, and a graceful
shutdown that drains in-flight RPCs. The learning lives in the modules marked
`TODO(Vx)`: the token bucket (V1, `token_bucket.py`), the sliding window (V2,
`sliding_window.py`), and the atomic Redis+Lua limiter the server actually calls
(V3, `redis_limiter.py`). See SPEC.md.

Scaffold state: this starts and serves. `grpcurl` can list the service through
reflection, `grpc.health.v1/Check` answers SERVING, and `GET /healthz` and
`GET /metrics` work. The first real `Check` raises a V3 `NotImplementedError` —
that is your worklist.

**One loop, two servers.** The gRPC server and the admin HTTP server share a
single event loop rather than running the admin side in a thread. That is the
honest arrangement: a blocking call anywhere shows up as *both* going quiet,
which is the feedback you want while you are being graded on "no blocking call
on the event loop". uvicorn is constructed with `loop="none"` because the loop
already exists — letting it install its own would fight `uvloop.run` for it.
"""

from __future__ import annotations

import asyncio
import signal

import common_telemetry
import grpc
import grpc.aio
import structlog
import uvicorn
import uvloop
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection
from redis.asyncio import Redis

from .config import Settings
from .pb import ratelimit_pb2 as pb
from .pb import ratelimit_pb2_grpc as rpc
from .redis_limiter import RedisLimiter
from .routes import create_admin_app
from .service import RateLimiterService
from .state import AppState

log = structlog.get_logger(__name__)

SERVICE_NAME = pb.DESCRIPTOR.services_by_name["RateLimiter"].full_name

SHUTDOWN_GRACE_SECONDS = 5.0
"""How long a stopping server waits for in-flight RPCs before cutting them off.

Long enough to finish work already accepted, short enough that a rolling deploy
doesn't stall on one hung request.
"""


def build_state(settings: Settings) -> AppState:
    """Assemble the runtime. Separate from `serve` so tests can build one."""
    # `from_url` hands back a pooled client, not a connection: concurrent
    # coroutines each check out their own, so one slow key can't block the rest.
    # The pool bound is a graded decision -- see `Settings.redis_max_connections`.
    # redis-py types `from_url`'s **kwargs loosely, so pyright cannot see the
    # keyword arguments below; the returned client is typed.
    client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )
    limiter = RedisLimiter(
        client,
        settings.limit,
        settings.algorithm,
        fail_open=settings.fail_open,
        key_ttl_seconds=settings.key_ttl_seconds,
    )
    return AppState(settings=settings, redis=client, limiter=limiter)


def build_grpc_server(state: AppState) -> tuple[grpc.aio.Server, health.HealthServicer]:
    """Construct the gRPC server with the limiter, health, and reflection on it."""
    # grpc-stubs types `aio.server`'s handler/interceptor generics as bare, so
    # pyright can't infer them at the call site. The returned Server is typed.
    server = grpc.aio.server(  # pyright: ignore[reportUnknownMemberType]
        # Backpressure, not an OOM: past this many in-flight RPCs the server
        # queues rather than accepting unbounded work. A limiter that falls over
        # under load has failed at its one job.
        maximum_concurrent_rpcs=state.settings.max_concurrent_rpcs,
    )
    rpc.add_RateLimiterServicer_to_server(RateLimiterService(state.limiter), server)

    # grpc.health.v1 -- what a gRPC client-side load balancer checks to decide
    # whether to keep sending this instance traffic.
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    # Server reflection, so `grpcurl` can introspect the service without being
    # handed a copy of the .proto.
    reflection.enable_server_reflection(
        (
            SERVICE_NAME,
            health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
            reflection.SERVICE_NAME,
        ),
        server,
    )
    return server, health_servicer


def set_health(servicer: health.HealthServicer, *, serving: bool) -> None:
    """Flip the health status for the whole server and for this service.

    The empty service name is the gRPC convention for "the server as a whole",
    which is what a client-side load balancer asks about; the named entry is for
    a caller that wants this specific service. Both, always, or a balancer and a
    client can disagree about whether this instance is taking traffic.
    """
    status = (
        health_pb2.HealthCheckResponse.SERVING
        if serving
        else health_pb2.HealthCheckResponse.NOT_SERVING
    )
    for name in ("", SERVICE_NAME):
        # grpcio-health-checking ships no stubs for its own generated
        # `health_pb2`, so the ServingStatus enum arrives as Unknown.
        servicer.set(name, status)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


async def serve(settings: Settings) -> None:
    """Run both servers until a signal arrives, then drain and exit."""
    state = build_state(settings)
    server, health_servicer = build_grpc_server(state)
    server.add_insecure_port(f"0.0.0.0:{settings.port}")

    admin = uvicorn.Server(
        uvicorn.Config(
            create_admin_app(state),
            host="0.0.0.0",
            port=settings.metrics_port,
            # The loop is already running under us; uvicorn must not install one.
            loop="none",
            # A Prometheus scrape every 15s does not need a log line each time.
            access_log=False,
            log_config=None,
        )
    )

    await server.start()
    admin_task = asyncio.create_task(admin.serve(), name="admin-http")

    # Only now do we report SERVING.
    set_health(health_servicer, serving=True)

    log.info(
        "rate limiter listening",
        grpc_addr=f"0.0.0.0:{settings.port}",
        admin_addr=f"0.0.0.0:{settings.metrics_port}",
        algorithm=str(settings.algorithm),
        fail_open=settings.fail_open,
        rate_per_sec=settings.rate_per_sec,
        burst=settings.burst,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler, not signal.signal: the handler runs as a loop
        # callback rather than interrupting arbitrary bytecode, so it can touch
        # asyncio objects safely. SIGTERM is the one that matters -- it is what
        # `docker stop` and Kubernetes send.
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutdown signal received", grace_seconds=SHUTDOWN_GRACE_SECONDS)
    # NOT_SERVING first, then drain: a load balancer that is watching gets to
    # stop sending new work before the door closes on the work already inside.
    set_health(health_servicer, serving=False)

    admin.should_exit = True
    await server.stop(SHUTDOWN_GRACE_SECONDS)
    await admin_task
    await state.redis.aclose()
    log.info("shutdown complete")


def main() -> None:
    settings = Settings()
    common_telemetry.init(settings.log_level)
    log.info(
        "starting",
        hint=(
            'grpcurl -plaintext -d \'{"key":"user-1","cost":1}\' '
            f"localhost:{settings.port} ratelimit.v1.RateLimiter/Check"
        ),
    )
    # uvloop is the production loop. It is materially faster on the socket work
    # a limiter spends its life doing -- and it is also where implementation
    # gaps hide, so run it here rather than discovering the difference in prod.
    uvloop.run(serve(settings))


if __name__ == "__main__":
    main()
