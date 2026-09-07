"""Real-time media transport (RTP/RTCP over UDP) — entrypoint and wiring.

The plumbing is wired for you: config, telemetry, the UDP media socket, the
per-role session task, the admin/metrics HTTP server, and graceful shutdown. The
learning lives in the modules marked `TODO(Vx)`:

* V1 `rtp.py`        — the RTP header codec and FU-A packetize/depacketize
* V2 `jitter.py`     — the reorder + playout buffer that hides the jitter
* V3 `rtcp.py`       — RTCP, the NACK bitmask, and the retransmit cache
* V4 `congestion.py` — the bandwidth estimator and the pacer

Scaffold state: this starts and serves. `GET /healthz`, `/status` and `/metrics`
all answer immediately. As `ROLE=receiver` the media plane idles until the first
RTP datagram arrives, which reaches `RtpPacket.parse` and raises
`NotImplementedError`; as `ROLE=sender` it produces a synthetic frame within
33 ms and reaches `Packetizer.packetize`. Either way the session task ends, the
HTTP server keeps serving, `/healthz` stays green and `/readyz` goes red. That
message is your worklist. See SPEC.md.

## Two planes, one process

The admin plane is FastAPI under uvicorn, in the foreground. The media plane is
a UDP datagram endpoint and a session task, started and stopped inside the
lifespan.

That arrangement is doing real work rather than being tidy. `uvicorn.run`
installs the SIGTERM handler, so `docker stop` triggers uvicorn's graceful
shutdown — in-flight admin requests drain — and *then* runs the lifespan's
`finally`, which stops the session and closes the socket. Wire it the other way,
with the session in the foreground and uvicorn in a task, and the whole shutdown
contract becomes yours to reimplement, badly.

The order in that `finally` reads backwards from the promise the SPEC makes:
stop the session first, then release the socket. A socket closed while the
sender is still pacing packets onto it is a `sendto` on a closed transport for
whatever was in flight.

## What CPython costs you here, stated up front

The boss fight asks for ≥ 90% of lost packets recovered before deadline, 99.5%
of frames played on time with no stall over 300 ms, ≤ 150 ms of added latency,
a send rate within 15% of capacity, and flat RSS across five minutes. Those
numbers are **not** scaled down for Python. Where CPython cannot reach one,
**the gap is the finding**, and it belongs in `docs/14-benchmarks.md` with its
cause named.

The candidates are known in advance and worth looking for by name. At 1.5 Mbps
in 1200-byte packets this is ~150 packets/sec each way — modest — but every one
allocates an `RtpHeader`, an `RtpPacket` and a payload slice, and the jitter
buffer holds a few hundred of them at a time, which is a steady feed into the
GC's nursery and a plausible source of a p99 tail that looks like network
jitter. The whole media plane is one thread, so a slow `insert` delays the
playout tick *and* the HTTP server, and there is no second core to spill onto.
The 10 ms playout tick is a wakeup 100 times a second whose scheduling latency
lands directly in the added-latency budget you have 150 ms of.

"Python is slow" is not a finding. "The playout tick's p99 wakeup latency is
14 ms under load because the receive loop holds the thread through a 300-packet
`missing()` scan, and tracking gaps incrementally in `insert` cuts it to 2 ms"
is.

`uvicorn[standard]` installs uvloop and `loop="auto"` picks it, so the process
you ship runs a different event loop from the one pytest runs on. That is why
the Definition of done asks you to boot the container — and why `udp.py` is
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

from . import metrics, session
from .config import Role, Settings
from .errors import install_error_handlers
from .routes import router
from .state import AppState, session_failure, unwrap_group
from .udp import MediaSocket, open_media_socket

logger = structlog.get_logger(__name__)

__all__ = ["create_app", "main"]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn lets in-flight admin requests finish after SIGTERM.

Admin calls are trivially fast, so this is really a budget for the lifespan
teardown that follows: stopping the session and closing the socket. Short,
because there is nothing here worth waiting on — unlike a broker drain, a media
packet held past SIGTERM is already too late to play."""

SESSION_SHUTDOWN_BUDGET = 5.0
"""Seconds to wait for the session to finish after cancellation. Short: it holds
nothing durable, so anything longer means it is wedged."""


def _log_session_exit(task: asyncio.Task[None]) -> None:
    """Surface a dead media session the moment it dies.

    An asyncio task that raises does so *silently*: nothing is printed until
    someone awaits it (here, shutdown) or the garbage collector complains. For
    the task that IS the media plane, that silence is indistinguishable from an
    idle link — you would watch a flat `/metrics` for ten minutes before
    noticing. This callback is the fix, and it is the habit to keep for every
    long-lived task you ever spawn.

    On the bare scaffold this fires with a `NotImplementedError` from whichever
    vertical the pipeline reached first, which is exactly the message you want
    at the top of the log. It arrives wrapped in an `ExceptionGroup` because the
    session runs its concerns in a `TaskGroup`, so the group is unwrapped here
    rather than making you read the nesting.
    """
    exc = session_failure(task)
    if exc is None:
        return
    logger.error(
        "media session died; no packets are moving",
        error=str(exc) or type(exc).__name__,
        kind=type(exc).__name__,
    )


async def _run_session(socket: MediaSocket, config: Settings) -> None:
    """Dispatch to the role's loop.

    A sender with no usable `REMOTE_ADDR` fails here rather than at the first
    `sendto`, so the error names the missing variable instead of surfacing as a
    `TypeError` on a `None` address twenty packets in.
    """
    if config.role is Role.SENDER:
        remote = config.remote
        if remote is None:
            raise ValueError("ROLE=sender requires REMOTE_ADDR as host:port")
        await session.run_sender(socket, config, remote)
    else:
        await session.run_receiver(socket, config)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app, with the media plane on its lifespan.

    A factory rather than a module-level `app` so tests can construct an
    independent transport on an ephemeral UDP port without touching the
    environment — and so a sender and a receiver can run in one process, which
    is how you loop the whole pipeline back on itself without a second machine.
    """
    config = settings if settings is not None else Settings()

    metrics.preregister()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # An exit stack rather than a nested `async with`, because the socket
        # has to outlive the `yield` and a context manager cannot straddle one
        # on its own. It also guarantees the socket closes even if starting the
        # session raises.
        async with AsyncExitStack() as stack:
            media = await stack.enter_async_context(
                open_media_socket(config.rtp_port, config.rtp_inbox)
            )
            # Held in a local for the whole lifespan: a bare `create_task`
            # result that nobody keeps can be garbage-collected mid-flight, and
            # here that means the media plane silently stopping while
            # `/healthz` still answers `ok`.
            task = asyncio.create_task(_run_session(media, config), name="media-session")
            task.add_done_callback(_log_session_exit)

            app.state.app_state = AppState(settings=config, media=media, session=task)
            try:
                yield
            finally:
                # Stop the session before the socket goes away — see the module
                # docstring on why this order is not cosmetic.
                task.cancel()
                try:
                    async with asyncio.timeout(SESSION_SHUTDOWN_BUDGET):
                        await task
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as exc:  # noqa: BLE001
                    # The session already died (a vertical's
                    # NotImplementedError while you build it). Report it, but
                    # never let it turn a clean shutdown into a failed one.
                    cause = unwrap_group(exc) or exc
                    logger.warning(
                        "media session ended with an error",
                        error=str(cause),
                        kind=type(cause).__name__,
                    )
                logger.info("shutdown complete")

    app = FastAPI(
        title="media-transport",
        summary="RTP, a jitter buffer, RTCP/NACK recovery and congestion control (project 14).",
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
    hint = (
        f"REMOTE_ADDR={config.remote_addr or '<unset — required for ROLE=sender>'}"
        if config.role is Role.SENDER
        else f"send RTP to 127.0.0.1:{config.rtp_port}/udp"
    )
    logger.info(
        "starting",
        role=config.role.value,
        http_addr=f"0.0.0.0:{config.http_port}",
        rtp_addr=f"0.0.0.0:{config.rtp_port}",
        hint=hint,
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
