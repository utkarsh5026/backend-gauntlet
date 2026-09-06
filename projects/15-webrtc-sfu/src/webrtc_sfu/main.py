"""WebRTC SFU (Selective Forwarding Unit) — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the muxed UDP media socket,
the media pump, the signaling + admin HTTP server, and graceful shutdown. The
learning lives in the modules marked `TODO(Vx)`:

* V1 `ice.py`       — the STUN codec + ICE-lite agent that makes the SFU reachable
* V2 `forward.py`   — the per-subscriber rewriter that keeps one continuous stream
* V3 `simulcast.py` — the layer selector that picks a quality per subscriber
* V4 `bwe.py`       — the bandwidth estimator that tells V3 what fits

Scaffold state: this starts and serves. `GET /healthz`, `/status`, `/rooms` and
`/metrics` all answer, and you can create publishers and subscribers over the
signaling API immediately. The media plane idles until a real client
ICE-connects; the first STUN check it sends reaches `StunMessage.parse` and
raises `NotImplementedError`, which ends the pump task while the HTTP server
keeps serving — so `/healthz` stays green and `/readyz` goes red. That message
is your worklist. See SPEC.md.

## Two planes, one process

The signaling + admin plane is FastAPI under uvicorn, in the foreground. The
media plane is a UDP datagram endpoint and a pump task, started and stopped
inside the lifespan.

That arrangement is doing real work rather than being tidy. `uvicorn.run`
installs the SIGTERM handler, so `docker stop` triggers uvicorn's graceful
shutdown — in-flight signaling requests drain — and *then* runs the lifespan's
`finally`, which stops forwarding and closes the socket. Wire it the other way,
with the pump in the foreground and uvicorn in a task, and the whole shutdown
contract becomes yours to reimplement, badly.

The order in that `finally` reads backwards from the promise the SPEC makes:
stop forwarding first, then release the socket. A socket closed while the pump
is still dispatching is a `sendto` on a closed transport for every subscriber of
whatever packet happened to be in flight.

## What CPython costs you here, stated up front

The boss fight asks for 50× fan-out with a forwarding p99 under 10 ms, layer
convergence within 3 s, flat CPU and flat RSS across a five-minute run. Those
numbers are not scaled down for Python, deliberately — where CPython cannot
reach one, **the gap is the finding**, and it belongs in
`docs/15-benchmarks.md` with its cause named.

The candidates are known in advance and worth looking for by name. Fan-out
allocates one `bytearray` per subscriber per packet, so 50 subscribers at
1500 pps is 75,000 allocations a second straight into the GC's nursery. The
whole media plane is one thread, so a slow `handle_rtp` delays the HTTP server
and every other subscriber, and there is no second core to spill onto. `sendto`
fifty times in a row is fifty syscalls with interpreter dispatch between each.
"Python is slow" is not a finding. "The per-subscriber `bytearray` copy is 38%
of the flamegraph at 50 subscribers, and a `memoryview` over a shared buffer
with a per-subscriber 12-byte header patch cuts it to 11%" is.

`uvicorn[standard]` installs uvloop and `loop="auto"` picks it, so the process
you ship runs a different event loop from the one pytest runs on. That is why
the Definition of done asks you to boot the container — and why `pump.py` is
built on `create_datagram_endpoint` rather than a raw socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import common_telemetry
import structlog
import uvicorn
from fastapi import FastAPI

from . import metrics, pump
from .config import Settings
from .errors import install_error_handlers
from .routes import router
from .sfu import Sfu
from .state import AppState

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn lets in-flight signaling requests finish after SIGTERM.

Signaling calls are trivially fast, so this is really a budget for the lifespan
teardown that follows: stopping the pump and closing the media socket. Short,
because there is nothing here worth waiting on — unlike a broker drain, a media
packet held past SIGTERM is already too late to play."""

PUMP_SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for the pump to finish after cancellation. Short: it holds
nothing and awaits only the inbox, so anything longer means it is wedged."""


def _log_pump_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead media pump the moment it dies.

    An asyncio task that raises does so *silently*: nothing is printed until
    someone awaits it (here, shutdown) or the garbage collector complains. For
    the task that IS the media plane, that silence is indistinguishable from an
    idle room — you would watch a flat `/metrics` for ten minutes before
    noticing. This callback is the fix, and it is the habit to keep for every
    long-lived task you ever spawn.

    On the bare scaffold this fires with a `NotImplementedError` from whichever
    vertical the first real client reached, which is exactly the message you
    want at the top of the log.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "media pump died; the SFU is no longer forwarding",
            error=str(exc) or type(exc).__name__,
            kind=type(exc).__name__,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app, with the media plane on its lifespan.

    A factory rather than a module-level `app` so tests can construct an
    independent SFU on an ephemeral media port without touching the environment
    — and so two SFUs can run in one process, which is how you test a peer
    against a second instance without a second machine.
    """
    config = settings if settings is not None else Settings()

    metrics.preregister()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        sfu = Sfu(config)
        # An exit stack rather than a nested `async with`, because the socket
        # has to outlive the `yield` and a context manager cannot straddle one
        # on its own. It also guarantees the socket closes even if starting the
        # pump raises.
        async with AsyncExitStack() as stack:
            media = await stack.enter_async_context(
                pump.open_media_socket(config.media_port, config.media_inbox)
            )
            # Held in a local for the whole lifespan: a bare `create_task`
            # result that nobody keeps can be garbage-collected mid-flight, and
            # here that means the media plane silently stopping while
            # `/healthz` still answers `ok`.
            task = asyncio.create_task(pump.run(media, sfu), name="media-pump")
            task.add_done_callback(_log_pump_exit)

            app.state.app_state = AppState(
                settings=config,
                sfu=sfu,
                media=media,
                pump=task,
            )
            try:
                yield
            finally:
                # Stop forwarding before the socket goes away — see the module
                # docstring on why this order is not cosmetic.
                task.cancel()
                try:
                    async with asyncio.timeout(PUMP_SHUTDOWN_BUDGET):
                        await task
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as exc:  # noqa: BLE001
                    # The pump already died (a vertical's NotImplementedError
                    # while you build it). Report it, but never let it turn a
                    # clean shutdown into a failed one.
                    logger.warning("media pump ended with an error", error=str(exc))
                logger.info("shutdown complete")

    app = FastAPI(
        title="webrtc-sfu",
        summary="ICE/STUN, per-subscriber RTP rewriting, simulcast and BWE (project 15).",
        lifespan=lifespan,
    )
    # Outermost: every log line emitted while serving carries the request id.
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
        media_addr=f"0.0.0.0:{config.media_port}",
        advertised=f"{config.public_ip}:{config.media_port}",
        hint=f"curl -XPOST localhost:{config.http_port}/rooms/demo/publish -d '{{...}}'",
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
