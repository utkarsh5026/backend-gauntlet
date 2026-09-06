"""VOD streaming server (HLS/DASH) — entrypoint and wiring.

The plumbing is wired for you: config, scanning the media library, the FastAPI
app, CORS, `/metrics`, and graceful shutdown. The learning lives in the modules
marked `TODO(Vx)`: the ISO-BMFF demuxer (V1, `isobmff.py`), the fMP4/CMAF
segmenter (V2, `segment.py`), the HLS/DASH manifest generator (V3,
`manifest.py`), and byte-range delivery (V4, `delivery.py`). See SPEC.md.

There is no external dependency and no docker-compose here: **the filesystem is
the source**. Scaffold state: this starts and serves. `GET /healthz`,
`GET /assets` and `GET /metrics` work; the first playlist or segment request
raises `NotImplementedError` naming the vertical it needs, and that message is
your worklist.

## Run it

```bash
make setup && make sync
make fixture              # generate a two-rendition test asset with ffmpeg
make run                  # the server on :8080
curl localhost:8080/assets
```

## Where graceful shutdown actually happens

Almost entirely in uvicorn, and that is the right answer here. On SIGTERM it
stops accepting connections and gives in-flight requests
`timeout_graceful_shutdown` seconds to finish; the lifespan's `finally` then runs
with nothing left to drain. This server holds no connection pool and no
background task to unwind — the catalog is a dict and the source mappings are
opened and closed inside a single request — so there is genuinely nothing to
close, and saying so explicitly is better than inventing ceremony.

What *does* need the budget is the SPEC's "no mid-segment connection drops"
criterion. A client pulling a 6-second segment over a slow link can legitimately
still be reading when the signal arrives, and cutting it produces a visible stall
in a player rather than a clean end of stream. Hence the generous number below.

## A ceiling worth measuring

Demuxing and muxing are CPU-bound pure-Python work, dispatched to a thread pool
(see `catalog.py`) so they cannot block the event loop. The GIL means those
threads interleave rather than run in parallel, so segment-cutting throughput on
CPython will not reach what the Rust version could. **Do not scale the SPEC's
numbers down.** Find where the wall is, work out which of the four usual suspects
put it there — GIL contention, GC pressure from a per-frame `Sample` object,
allocation in the mux loop, or a blocking call that escaped onto the loop — and
write it up in `docs/11-benchmarks.md`. That finding is the deliverable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .catalog import Catalog
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 20
"""How long uvicorn lets in-flight requests finish after SIGTERM.

Sized against the thing being served rather than a habit: a request here is a
segment download, and a client on a poor connection can legitimately still be
reading one for several times its playback duration. It should comfortably exceed
the target segment length — see `TARGET_SEGMENT_SECS` — or a deploy truncates
exactly the requests that were most worth finishing."""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    A factory rather than a module-level `app` so tests can stand up several
    independent servers in one process over different media directories — which
    is what testing a library scanner honestly requires.
    """
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Scanning is blocking directory I/O. It runs once, here, before uvicorn
        # accepts a connection — so `GET /assets` is honest from the first
        # request — and in a thread, so a library on a slow or networked
        # filesystem cannot stall the loop while it starts.
        catalog = await asyncio.to_thread(Catalog.load, cfg.media_dir, cfg.target_segment_secs)
        app.state.app_state = AppState(settings=cfg, catalog=catalog)

        logger.info(
            "media library ready",
            media_dir=str(cfg.media_dir),
            assets=len(catalog.asset_names()),
            target_segment_secs=cfg.target_segment_secs,
        )
        try:
            yield
        finally:
            # Nothing to unwind: no pool, no background task, and every source
            # mapping is opened and closed inside the request that needed it.
            # uvicorn has already drained in-flight segment reads by the time
            # this runs — see the module docstring.
            logger.info("shutdown complete")

    app = FastAPI(
        title="vod-streaming",
        summary="An HLS/DASH VOD server built from the box tree up (project 11).",
        lifespan=lifespan,
    )
    # Outermost, so every log line emitted while packaging carries the request id
    # and the id comes back on the response — which is what lets you take an id
    # off a failed segment fetch and find the cut that produced it in the log.
    app.add_middleware(common_telemetry.RequestIdMiddleware)

    # Browser players (hls.js / dash.js) fetch cross-origin, and byte-range reads
    # need the range headers *exposed* or the player cannot see them.
    #
    # TODO(protocols, horizontal): this is wide open. Tighten `allow_origins` to
    # what actually needs it, and add
    #   expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"]
    # so cross-origin range reads work — without it a browser hides those three
    # from the page and seeking silently breaks while everything else looks fine.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )
    install_error_handlers(app)

    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    logger.info(
        "starting",
        addr=f"0.0.0.0:{cfg.port}",
        hint="GET /assets to browse; /vod/{asset}/master.m3u8 to play",
    )

    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        # "auto" picks uvloop, which uvicorn[standard] installs. Worth knowing
        # that this is *not* the loop pytest runs on, which is why the SPEC's
        # Definition of done asks you to boot the container: some bugs exist under
        # only one of the two.
        loop="auto",
        # RequestIdMiddleware already emits one structured line per request;
        # uvicorn's access log would double the I/O on the hot path.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
