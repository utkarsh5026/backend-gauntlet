"""BitTorrent client + seeder — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the `Client` engine, the
optional inbound-peer listener and its choke scheduler, the HTTP control plane,
and graceful shutdown. The learning lives in the modules marked `TODO(Vx)`:

* V1 `bencode.py`   — the wire's data format, and the byte-exact round-trip
* V2 `metainfo.py`  — parse `.torrent` / `magnet:` and compute the infohash
* V3 `tracker.py`   — announce over HTTP *and* UDP to discover peers
* V4 `peer.py`      — the peer wire protocol over raw TCP
* V5 `download.py`  — rarest-first selection, SHA-1 verify, write the file
* V6 `seeder.py`    — serve pieces under a choke algorithm (upload slots)

Scaffold state: this starts and serves. `GET /healthz`, `/config`, `/torrents`
and `/metrics` all answer. `POST /torrents` raises `NotImplementedError` the
moment it reaches the metainfo parser (V2), and with `RUN_SEEDER=true` the first
inbound peer trips V6 — which ends *that* session while the process keeps
serving. Those messages are your worklist. See SPEC.md.

## Three servers, one process

The HTTP control plane is FastAPI under uvicorn. The seeder is
`asyncio.start_server` on the peer port. The tracker's UDP socket is a datagram
endpoint opened per announce. Uvicorn runs in the foreground and owns the signal
handling; everything else starts and stops inside the lifespan.

That arrangement is doing real work rather than being tidy: `uvicorn.run`
installs the SIGTERM handler, so `docker stop` triggers the lifespan's
`finally`, which is what drains peer sessions and announces `stopped` to the
trackers. Wire it the other way — the peer listener in the foreground, uvicorn
in a task — and shutdown becomes yours to reimplement, badly.

Shutdown order in that `finally` reads backwards from the promise. Stop
scheduling unchokes, stop accepting and drain the peers that are mid-send, and
only *then* close the client — because a `stopped` announced while you are still
serving blocks is a claim you spend the next five seconds making false, and a
disk pool shut down before the last write is a half-written piece.

## What CPython costs you here, stated up front

The boss fight asks for 50 concurrent leechers, 500 MB/s aggregate over
loopback, RSS under 200 MB and a p99 time-to-first-block of 250 ms. Those
numbers are not scaled down for Python, deliberately — where CPython cannot
reach one, **the gap is the finding**, and it belongs in
`docs/19-benchmarks.md` with its cause named: the GIL serializing message
framing across fifty sessions, a `os.pread` on the event loop, allocation churn
copying 16 KiB blocks through three buffers on their way to the socket, GC
pauses under a large piece table. "Python is slow" is not a finding. "Framing
allocates two `bytes` objects per block and at 500 MB/s that is 32000
allocations a second and the top frame in the flamegraph" is.

`uvicorn[standard]` installs uvloop and `loop="auto"` picks it, so the process
you ship runs a different event loop from the one pytest runs on. That is why
the Definition of done asks you to boot the container — and why `tracker.py`'s
UDP plumbing is wired the way it is, since uvloop does not implement the
`loop.sock_*` family that a raw-socket version would reach for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import metrics
from .client import Client
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .seeder import Seeder, choke_loop
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 20
"""How long uvicorn lets in-flight HTTP requests finish after SIGTERM.

The control plane's requests are trivially fast, so this is really a budget for
the *lifespan* teardown that follows them: draining peer sessions and announcing
`stopped` to every tracker. Generous, because the alternative to waiting is
lingering in the swarm as a peer that answers nothing, and no deploy is fast
enough to be worth that."""

CHOKE_SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for the choke scheduler to stop after cancellation. Short: it
sleeps between rounds and holds nothing, so anything longer means it is stuck."""


def _preregister_metrics() -> None:
    """Create every known labelled child so it exports at zero from startup.

    A labelled Prometheus metric does not exist until something creates that
    child, so without this `bt_pieces_verified_total{result="failed"}` is simply
    **absent** until the first lying peer — and "absent" is not "zero". A
    dashboard shows a gap, `rate()` returns no data, and an alert written as
    `rate(...) > 0` never fires while an alert written as `absent(...)` fires
    constantly for the entirely healthy reason that nothing has gone wrong yet.

    Enumerating the label values you know about is the fix, and it is only
    possible because they are a closed set: two outcomes, two transports. That
    is the same property that makes them safe as labels at all — see
    `metrics.py` on why a peer address is not.

    Importing `metrics` for its module-level declarations is the other half:
    `prometheus_client` collectors register themselves into the default registry
    at import time, so a metrics module nothing imports is a `/metrics` endpoint
    that is empty until some unrelated code happens to pull it in.
    """
    for result in ("ok", "failed"):
        metrics.PIECES_VERIFIED_TOTAL.labels(result=result)
    for transport in ("http", "udp"):
        for outcome in ("ok", "error"):
            metrics.ANNOUNCES_TOTAL.labels(transport=transport, result=outcome)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI control plane, with the engine and seeder on its lifespan.

    A factory rather than a module-level `app` so tests can stand up an
    independent client over a temp download directory without touching the
    environment — and, more importantly here, so two clients can run in one
    process on ephemeral peer ports and swarm *each other*, which is the only
    way to test V4 and V6 end to end without a second machine.
    """
    cfg = settings if settings is not None else Settings()

    _preregister_metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        client = Client(cfg)
        logger.info("client identity", peer_id=str(client.peer_id), peer_port=cfg.peer_port)

        seeder: Seeder | None = None
        choker: asyncio.Task[None] | None = None
        if cfg.run_seeder:
            seeder = Seeder(client, cfg)
            await seeder.start(port=cfg.peer_port)
            # Held in a local for the whole lifespan: a bare `create_task` result
            # that nobody keeps can be garbage-collected mid-flight, and here
            # that means the choke rotation silently stopping while /healthz
            # still answers `ok` — a seeder that quietly serves the same four
            # peers forever, which is precisely the boss fight's failure mode
            # arriving with no signal at all.
            choker = asyncio.create_task(choke_loop(seeder, cfg), name="choke")
            logger.info(
                "seeding enabled",
                upload_slots=cfg.upload_slots,
                max_peers=cfg.max_peers,
            )
        else:
            logger.info("seeder disabled (RUN_SEEDER=false): control plane + leecher only")

        app.state.app_state = AppState(settings=cfg, client=client, seeder=seeder)

        try:
            yield
        finally:
            # Order matters — see the module docstring. Stop scheduling, stop
            # serving, then release the engine.
            if choker is not None:
                choker.cancel()
                try:
                    async with asyncio.timeout(CHOKE_SHUTDOWN_BUDGET):
                        await choker
                except (TimeoutError, asyncio.CancelledError):
                    pass

            if seeder is not None:
                await seeder.close()

            await client.close()
            logger.info("shutdown complete")

    app = FastAPI(
        title="bittorrent",
        summary="A from-scratch BitTorrent client + seeder with an HTTP control plane.",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
    app.add_middleware(common_telemetry.RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.router.routes.extend(common_telemetry.metrics_routes())
    return app


def main() -> None:
    cfg = Settings()
    common_telemetry.init(cfg.log_level)
    logger.info(
        "starting",
        http_addr=f"0.0.0.0:{cfg.port}",
        peer_port=cfg.peer_port,
        download_dir=str(cfg.download_dir),
        seeding=cfg.run_seeder,
        hint=f"curl -X POST --data-binary @file.torrent localhost:{cfg.port}/torrents",
    )
    uvicorn.run(
        create_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
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
