"""Structured logging + metrics — the Python sibling of `crates/common-telemetry`.

Fully implemented on purpose (CLAUDE.md's `common-*` exception). Every project
gets the same three things so observability is never re-invented per project:

  * `init()`      — structlog wired to emit **JSON in production, colour on a tty**.
  * `RequestIdMiddleware` — a request id per request, bound into the log context
    so every line emitted while serving that request carries it automatically.
  * `metrics_router()` — `GET /metrics` in Prometheus text format.

The middleware is written as **raw ASGI** rather than Starlette's
`BaseHTTPMiddleware`. That is deliberate: `BaseHTTPMiddleware` wraps every
request in an anyio task pair, which shows up in the p99 these projects are
graded on, and reading the raw three-callable ASGI protocol is worth doing once.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "RequestIdMiddleware",
    "get_logger",
    "init",
    "metrics_routes",
    "request_id",
]

_REQUEST_ID_HEADER = b"x-request-id"


def init(level: str = "info", *, force_json: bool | None = None) -> None:
    """Configure structlog + stdlib logging for the process.

    `level` is a plain level name (`debug`/`info`/…). JSON is chosen
    automatically when stderr is not a tty (i.e. in Docker and in CI), which is
    what you want: humans get colour locally, log shippers get JSON in prod.
    """
    numeric = getattr(logging, level.strip().upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric)

    use_json = (not sys.stderr.isatty()) if force_json is None else force_json
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            # Pulls anything bound via bind_contextvars (the request id) into
            # every event dict — this is what makes per-request correlation work
            # without threading a logger through every call.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """A bound logger. Call after `init()`."""
    return structlog.get_logger(name)


def request_id() -> str | None:
    """The current request's id, if called while serving one."""
    bound = cast(dict[str, Any], structlog.contextvars.get_contextvars())
    value = bound.get("request_id")
    return value if isinstance(value, str) else None


class RequestIdMiddleware:
    """Assigns (or honours) a request id and logs one line per request.

    An inbound `x-request-id` is preserved so a trace survives across a hop —
    which matters in project 07, where one node forwards to another and you want
    both halves of the request under a single id.
    """

    def __init__(self, app: ASGIApp, *, access_log: bool = True) -> None:
        self.app = app
        self.access_log = access_log
        self._log = structlog.get_logger("http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        incoming = next((v for k, v in headers if k == _REQUEST_ID_HEADER), None)
        rid = incoming.decode("latin-1") if incoming else uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)

        started = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = cast(int, message["status"])
                raw = cast(list[tuple[bytes, bytes]], message.setdefault("headers", []))
                raw.append((_REQUEST_ID_HEADER, rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if self.access_log:
                self._log.info(
                    "request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=status_holder.get("status"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()


async def _metrics(_request: Request) -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def metrics_routes(path: str = "/metrics") -> list[Route]:
    """Routes exposing the default Prometheus registry.

    Returned as plain Starlette routes so this package doesn't depend on
    FastAPI — mount them with `app.router.routes.extend(metrics_routes())` or
    include them when constructing the app.
    """
    return [Route(path, cast(Callable[[Request], Awaitable[Response]], _metrics))]
