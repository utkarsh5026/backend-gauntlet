"""The RTMP ingest server — **wired**, not a vertical.

A raw-TCP listener: one `asyncio` task per publisher connection, each running a
`PublishSession` (V1 handshake + chunk reader, V2 state machine). This module
owns the parts of that which are plumbing: accepting, classifying how a session
ended, keeping a failure out of every other session, and shutting down.

## Why `asyncio.start_server` and not a raw socket

Production runs on **uvloop**; pytest runs on the stdlib loop. Streams
(`start_server`, `StreamReader`, `StreamWriter`) are implemented by both.
uvloop does **not** implement the `loop.sock_*` family, so an accept loop built
on a raw `socket` and `loop.sock_accept` passes every test and raises
`NotImplementedError` in the container — indistinguishable, in the log, from a
vertical you have not written yet. That is why the Definition of done asks you
to boot the container rather than trusting `make verify`.

## Backpressure is the reader's `limit`

Each connection's `StreamReader` is created with
`limit=RTMP_READ_BUFFER_BYTES`. Once more than twice that sits unread, the
transport **stops reading the socket**, the kernel's receive buffer fills, TCP
advertises a zero window, and the *publisher's* encoder stalls. A slow parser
therefore degrades its own broadcaster, not the server's heap — which is the
SPEC's "backpressure from a slow publisher" skill, delivered by a constructor
argument. `StreamWriter.drain()` is the same thing in the outbound direction.

## One failure domain per connection

Every way a session can end lands in exactly one branch below, and each one
closes *that* socket and nothing else:

* `EOFError` — the publisher hung up. Normal.
* `ProtocolError` — the publisher was wrong: a bad chunk, a bad key. Normal too,
  from the server's point of view; it is logged as a warning.
* `NotImplementedError` — **you** are not done yet. Logged as an error naming
  the function, and recorded on `last_failure` so `/status` shows your worklist
  without a trip to the log. This is the Python analogue of the Rust
  scaffold's `todo!()` panic, deliberately not swallowed as a protocol error.
* anything else — a bug. Logged with its traceback.

Each connection's task runs in its own copy of the context, so the
`rtmp_session` id bound into structlog's contextvars here appears on every log
line that session emits — and on no other session's.
"""

from __future__ import annotations

import asyncio
import itertools
import socket
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import structlog

from .config import Settings
from .errors import ProtocolError
from .live import LiveRegistry
from .metrics import RTMP_CONNECTIONS_TOTAL, RTMP_SESSIONS_ENDED_TOTAL
from .session import PublishSession

__all__ = ["RtmpIngest", "describe_failure", "open_rtmp_ingest"]

logger = structlog.get_logger(__name__)

CLOSE_BUDGET_SECONDS = 1.0
"""How long to wait for a closed socket to finish flushing. A peer that will not
read its last bytes does not get to hold a shutdown open."""


def describe_failure(exc: BaseException) -> str:
    """`"NotImplementedError: V1: … (rtmp.py:190 handshake)"` — the exception
    and the innermost frame it came from, which on a scaffold names the exact
    function to write next."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" ({Path(last.filename).name}:{last.lineno} {last.name})"
    return f"{type(exc).__name__}: {exc}{where}"


class RtmpIngest:
    """The RTMP listener and the sessions it is running."""

    def __init__(self, registry: LiveRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings
        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task[None]] = set()
        self._ids = itertools.count()
        self.last_failure: str | None = None
        """The most recent unbuilt-vertical or unexpected error, described."""

    @property
    def port(self) -> int:
        """The port actually bound — differs from `RTMP_PORT` when that is 0."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("rtmp ingest is not listening")
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection,
            host="0.0.0.0",
            port=self._settings.rtmp_port,
            limit=self._settings.rtmp_read_buffer_bytes,
        )
        logger.info(
            "rtmp ingest listening",
            rtmp_addr=f"0.0.0.0:{self.port}",
            hint=f"publish to rtmp://localhost:{self.port}/live/<key>",
        )

    async def stop(self, budget: float) -> None:
        """Stop accepting, cancel every session, and wait up to `budget` seconds.

        Cancelling a session runs its `finally`, which marks its stream ended —
        so any viewer still holding a blocking reload is woken, not stranded.

        TODO(horizontal, graceful shutdown): a cancelled publisher's forming
        segment is simply dropped here. The SPEC wants SIGTERM to look like a
        broadcast ending: the current segment finalized and the playlist closed
        with `#EXT-X-ENDLIST`, before the process goes away.
        """
        if self._server is None:
            return
        self._server.close()
        for task in self._sessions:
            task.cancel()
        if self._sessions:
            await asyncio.wait(self._sessions, timeout=budget)
        with suppress(TimeoutError):
            async with asyncio.timeout(budget):
                await self._server.wait_closed()
        logger.info("rtmp ingest stopped")

    async def _on_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None  # always called inside the task asyncio made for it
        self._sessions.add(task)
        session_id = next(self._ids)
        structlog.contextvars.bind_contextvars(rtmp_session=session_id)
        RTMP_CONNECTIONS_TOTAL.inc()

        # RTMP's control replies are tiny and the encoder blocks waiting on each
        # one; Nagle's algorithm would hold them back. asyncio already sets this
        # on TCP transports — set it explicitly so the intent survives a loop swap.
        sock = writer.get_extra_info("socket")
        if isinstance(sock, socket.socket):
            with suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        peer = writer.get_extra_info("peername")
        logger.info("rtmp connection accepted", peer=str(peer))

        # TODO(horizontal, security): nothing caps concurrent publishers yet.
        # Every accepted connection gets a task and a read buffer before it has
        # proven anything — this is the place to refuse the N+1th.
        session = PublishSession(session_id, reader, writer, self._registry, self._settings)
        reason = "closed"
        try:
            await session.run()
        except EOFError:
            reason = "closed"
        except ProtocolError as exc:
            reason = "protocol_error"
            logger.warning("rtmp session error, closing", error=str(exc), kind=type(exc).__name__)
        except NotImplementedError as exc:
            reason = "unimplemented"
            self.last_failure = describe_failure(exc)
            logger.error("rtmp session reached an unbuilt vertical", error=self.last_failure)
        except asyncio.CancelledError:
            reason = "shutdown"
            raise
        except Exception as exc:
            reason = "error"
            self.last_failure = describe_failure(exc)
            logger.exception("rtmp session crashed", error=self.last_failure)
        finally:
            RTMP_SESSIONS_ENDED_TOTAL.labels(reason=reason).inc()
            self._sessions.discard(task)
            writer.close()
            if reason != "shutdown":
                with suppress(Exception):
                    async with asyncio.timeout(CLOSE_BUDGET_SECONDS):
                        await writer.wait_closed()
            logger.info("rtmp connection ended", reason=reason, state=session.state.value)


@asynccontextmanager
async def open_rtmp_ingest(
    registry: LiveRegistry,
    settings: Settings,
    *,
    shutdown_budget: float = 5.0,
) -> AsyncGenerator[RtmpIngest]:
    """Run the ingest server for the duration of the `async with`."""
    ingest = RtmpIngest(registry, settings)
    await ingest.start()
    try:
        yield ingest
    finally:
        await ingest.stop(shutdown_budget)
