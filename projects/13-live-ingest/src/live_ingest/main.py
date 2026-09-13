"""Live ingest server (RTMP → LL-HLS) — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the shared live registry, the
raw-TCP RTMP server, the LL-HLS delivery app, and shutdown. The learning lives in
the modules marked `TODO(Vx)`:

* V1 `rtmp.py`                     — the handshake and the chunk-stream reader
* V2 `amf.py`, `session.py`, `flv.py` — AMF0, the publish state machine, codec setup
* V3 `fmp4.py`                     — the live fMP4 repackager
* V4 `llhls.py`                    — the LL-HLS playlist and blocking reload

Scaffold state: this starts and serves. `GET /healthz`, `/live` (empty),
`/status` and `/metrics` answer immediately. The moment a broadcaster connects,
its session reaches `rtmp.handshake` and raises `NotImplementedError`; that one
connection closes, the error is logged naming the function, `/status` shows it
as `last_session_failure`, and the server keeps accepting. That message is your
worklist. See SPEC.md.

## Two planes, one process, one loop

The delivery plane is FastAPI under uvicorn, in the foreground. The ingest
plane is an `asyncio` TCP server started and stopped inside the lifespan. Both
run on the same event loop and share one `LiveRegistry` — which is why a part a
publisher pushes can wake a viewer's held request with no queue, no thread and
no lock in between (see `live.py`).

That also means they share one **core**. A publisher's chunk parsing, V3's box
writing, and the burst of playlist renders every part triggers all take turns on
the same thread as every HTTP response. Nothing here is spilled onto a second
core, and that is the single most important fact about how this server will
behave against the Latency Wall.

## Shutdown order, and the part that is yours

`uvicorn.run` owns SIGTERM. On `docker stop` it stops accepting HTTP, lets
in-flight requests finish for up to `GRACEFUL_SHUTDOWN_SECONDS` — including held
blocking reloads, which is why that budget is longer than
`llhls.MAX_BLOCK_SECONDS` — and *then* runs the lifespan's teardown, which stops
the RTMP server and cancels its sessions. Each cancelled session marks its
stream ended on the way out.

Read that order again with a viewer in mind: during the HTTP drain the
publishers are still live, and a stream only ends *after* the HTTP side has
stopped serving. The graceful-shutdown checklist item — a SIGTERM that looks to
a viewer like a broadcast ending, with the last segment finalized and
`#EXT-X-ENDLIST` served — needs that order changed. It is left for you.

## What CPython costs you here, stated up front

The boss fight asks for ≤ 3 s glass-to-glass sustained for ten minutes, ≥ 95% of
blocking reloads served first time with p99 hold within ~1 part of
availability, flat RSS, and all of it with ≥ 200 concurrent players. Those
numbers are **not** scaled down for Python. Where CPython cannot reach one,
**the gap is the finding**, recorded with its cause in `docs/13-benchmarks.md`.

The suspects are known in advance. A 6 Mbps publisher at a 4 KiB chunk size is
~190 chunks a second, each a handful of `readexactly` awaits and small `bytes`
objects — steady allocator and GC traffic. Every ~300 ms, one `push_part` wakes
200 parked requests *in the same loop iteration*, so the p99 hold time is
really "how long until the 200th render got the thread". And `mdat` assembly
copies every sample's bytes once more than a zero-copy writer would.

"Python is slow" is not a finding. "p99 hold is 180 ms over one part because 200
wakes render the playlist 200 times; memoizing the render per edge cuts it to
6 ms" is.

`uvicorn[standard]` installs uvloop and `loop="auto"` picks it, so the process
you ship runs a different event loop from the one pytest runs on. That is why
the Definition of done asks you to boot the container, and why `ingest.py` is
built on `asyncio.start_server` rather than a raw socket.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import metrics
from .config import Settings
from .errors import install_error_handlers
from .ingest import open_rtmp_ingest
from .live import LiveRegistry
from .routes import router
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn drains in-flight HTTP after SIGTERM. Longer than
`llhls.MAX_BLOCK_SECONDS`, so a held blocking reload finishes instead of being
cut mid-response."""

RTMP_SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for cancelled publisher sessions to unwind."""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app, with the RTMP server on its lifespan.

    A factory rather than a module-level `app` so tests can stand up an
    independent server on an ephemeral RTMP port without touching the
    environment.
    """
    config = settings if settings is not None else Settings()

    metrics.preregister()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if not config.allowed_keys:
            logger.warning("STREAM_KEYS is empty: ANY key may publish (dev only, never in prod)")
        registry = LiveRegistry(config)
        async with open_rtmp_ingest(
            registry, config, shutdown_budget=RTMP_SHUTDOWN_BUDGET
        ) as ingest:
            app.state.app_state = AppState(settings=config, registry=registry, ingest=ingest)
            yield
        logger.info("shutdown complete")

    app = FastAPI(
        title="live-ingest",
        summary="RTMP ingest, live fMP4 remux and Low-Latency HLS delivery (project 13).",
        lifespan=lifespan,
    )
    # Browser LL-HLS players (hls.js) fetch cross-origin.
    # TODO(horizontal): tighten from "*" — which origins may embed your streams
    # is a policy decision, not boilerplate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )
    # Added last, so outermost: every log line emitted while serving — including
    # a CORS preflight — carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    config = Settings()
    common_telemetry.init(config.log_level)
    logger.info(
        "starting",
        http_addr=f"0.0.0.0:{config.http_port}",
        rtmp_port=config.rtmp_port,
        hint=f"play http://localhost:{config.http_port}/live/<key>/index.m3u8",
    )
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.http_port,
        # "auto" picks uvloop, which uvicorn[standard] installs — and which is
        # *not* the loop pytest runs on. See the module docstring.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request;
        # uvicorn's own access log would just double the I/O.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
