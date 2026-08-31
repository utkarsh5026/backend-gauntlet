"""The RESP server: accept TCP connections and turn commands into engine calls.

This is the glue between the wire (V1, `resp.py`) and the store (`engine.py`).
The accept loop, the per-connection read/dispatch/reply loop, the command table
and the drain are **wired**. The two things they lean on are the unbuilt parts:

* `resp.parse_command` / `resp.encode` — the V1 codec. Nothing parses until you
  write it, so the first command a client sends trips V1;
* `engine.get` / `set` / `delete` — the LSM read and write paths (V2 -> V7).

Scaffold behavior: a connection is accepted and stays open, and the first
*command* raises `NotImplementedError`, which ends that connection's task while
the server keeps running. That is the intended state, and the exception names
the vertical to build.

## Why this is not FastAPI

`redis-cli` speaks RESP over a raw socket. There is no HTTP framing to reuse, no
ASGI, no router — so the data plane is `asyncio.start_server`, and the FastAPI
app in `main.py` is only the observability sidecar. That split (raw protocol
server + HTTP sidecar) is the same one project 19 makes, and it is what real
databases do: the thing that serves data and the thing that reports on it are
different servers on different ports, so a scrape can never contend with a read.

## The three asyncio facts this file is built on

**`reader.read(n)` returns what has arrived, not `n` bytes.** It is not
`readexactly`. A partial frame is the *normal* case, not an error, which is
exactly why V1's parser contract is "return `None` and consume nothing".

**`writer.write()` does not block and does not send.** It buffers. `await
writer.drain()` is the backpressure point: it returns immediately while the
transport's buffer is below the high-water mark, and suspends when it is not. A
server that never drains will happily buffer an unbounded reply backlog for a
client that has stopped reading — which is a memory leak with a network trigger.
That is why the loop below drains once per batch.

**Cancelling a task blocked in `read()` is safe; cancelling one mid-command is
not.** The drain below leans on that: it stops accepting, lets every connection
finish the batch it is in, and only then cancels whatever is still parked
waiting for bytes. That is the difference between "graceful shutdown" and
"closed the sockets and hoped".
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

import structlog

from . import resp
from .config import Settings
from .errors import AppError, NoAuth, ProtocolError, WrongPass
from .resp import Command, Error, Reply

if TYPE_CHECKING:
    from .engine import Engine

__all__ = ["Connection", "RespServer", "dispatch"]

logger = structlog.get_logger(__name__)

READ_CHUNK = 64 * 1024
"""Bytes requested per socket read. Large enough that a pipelined burst arrives
in one syscall, small enough that a slow client does not reserve much."""

DRAIN_TIMEOUT = 5.0
"""Seconds to let in-flight connections finish their current batch on shutdown
before they are cancelled."""

NO_AUTH_REQUIRED = frozenset({"AUTH", "HELLO", "QUIT"})
"""The only commands allowed before a successful `AUTH` when `REQUIREPASS` is
set. `HELLO` is here because clients send it during the handshake to negotiate
the protocol version, and refusing it means the client never gets far enough to
authenticate."""


class Connection:
    """Per-connection state: whether it has authenticated, and its read buffer.

    One of these per coroutine, never shared. That is the whole reason the
    server needs no locking around client state — the state lives in the task
    that owns it, and there is exactly one task per socket.
    """

    __slots__ = ("authenticated", "buf", "busy", "out", "peer")

    def __init__(self, peer: str, *, authenticated: bool) -> None:
        self.peer = peer
        self.authenticated = authenticated
        self.buf = bytearray()
        """Unparsed inbound bytes. May hold a partial frame, or several whole
        ones — see V1's contract in `resp.py`."""
        self.out = bytearray()
        """Replies accumulated for this batch, written in one go. Batching is
        most of why pipelining is fast: N commands cost one write, not N."""
        self.busy = False
        """True while this connection has work in flight — from the moment a read
        returns bytes until its replies are on the wire. False means the task is
        parked in `read()` with nothing to lose, which is what lets shutdown
        cancel it instantly instead of waiting out the drain budget."""


class RespServer:
    """The RESP data plane.

    Constructed in the lifespan, started with `start()`, stopped with `close()`.
    """

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self._server: asyncio.Server | None = None
        self._connections: dict[asyncio.Task[None], Connection] = {}
        """Live connections, keyed by the task serving each — so shutdown can ask
        each one whether it is mid-command before deciding to cancel it."""
        self._closing = False
        self.port = 0
        """The port actually bound, filled in by `start()`. Not the same as
        `settings.resp_port`, which may be 0 meaning "any free port"."""

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def start(self, host: str = "0.0.0.0", port: int | None = None) -> int:
        """Bind and begin accepting. Returns the bound port, which is how a test
        can ask for port 0 and still know where to connect."""
        self._server = await asyncio.start_server(
            self._handle,
            host,
            port if port is not None else self.settings.resp_port,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        logger.info("RESP server listening", port=self.port, hint=f"redis-cli -p {self.port} ping")
        return self.port

    async def close(self) -> None:
        """Stop accepting, let in-flight commands finish, then cancel the rest.

        The three steps are the graceful-shutdown criterion, in order. Note that
        flushing the WAL is *not* here — it belongs to the engine and happens
        after this returns, because a WAL flushed before the last command
        finishes is a WAL that is missing it.
        """
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        if not self._connections:
            return

        # Idle connections are cancelled *now*. A task whose `busy` is False is
        # parked in `read()` with no command in flight and no reply owed, so
        # cancelling it loses nothing — and waiting for it would mean waiting the
        # whole drain budget for a client that is doing nothing, which is how a
        # `docker stop` turns into a ten-second hang with an idle `redis-cli`
        # attached.
        #
        # Reading every flag and then cancelling needs no lock, and that is not
        # luck: there is no `await` between the check and the cancel, so no
        # connection can go from idle to busy in the middle of this loop. Put an
        # `await` in here and that stops being true.
        idle = [task for task, conn in self._connections.items() if not conn.busy]
        for task in idle:
            task.cancel()

        # Busy connections get to finish the batch they are in — that is the
        # "finishes in-flight commands" half of the criterion.
        done, pending = await asyncio.wait(self._connections, timeout=DRAIN_TIMEOUT)
        if pending:
            logger.warning("connections did not finish in time", count=len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info(
            "connections drained",
            finished=len(done),
            idle_cancelled=len(idle),
            timed_out=len(pending),
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one connection until the peer closes it or the server drains.

        TODO(security · connection cap): redis's `maxclients`. Bound the number
        of concurrent connections — an `asyncio.Semaphore` acquired here, or a
        check against `connection_count` that replies `-ERR max number of
        clients reached` and closes. Without it a connection flood exhausts file
        descriptors and memory, and the failure mode is that the process stops
        accepting *anything*, including the connection you would have used to
        diagnose it.

        TODO(observability): bump `metrics.CONNECTED_CLIENTS` here and decrement
        it in the `finally` — in the `finally` specifically, because a
        connection that dies from an exception still needs to stop being
        counted, and a gauge that only goes up is worse than no gauge.
        """
        peer = _peer_name(writer)
        conn = Connection(peer, authenticated=not self.settings.auth_required)
        task = asyncio.current_task()
        if task is not None:
            self._connections[task] = conn
        try:
            await self._serve(conn, reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            # The client went away mid-write. Normal, and not worth an error
            # line — under the boss fight's load generator it is how every run
            # ends.
            logger.debug("connection reset", peer=peer)
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            # The scaffold's expected state: V1's codec, or an engine path, is
            # not built yet. Named at info so the first `redis-cli ping` tells
            # you what to build rather than looking like a crash.
            logger.info("connection hit an unbuilt vertical", peer=peer, detail=str(exc))
        except Exception as exc:
            logger.warning("connection failed", peer=peer, error=str(exc), kind=type(exc).__name__)
        finally:
            if task is not None:
                self._connections.pop(task, None)
            writer.close()
            # Swallowed deliberately: the peer may already be gone, and failing
            # to close a socket that is already closed is not a thing worth
            # logging on every disconnect.
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _serve(
        self,
        conn: Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read, drain every complete command out of the buffer, reply, repeat.

        Read *first*, then parse: a client that connects and says nothing is a
        perfectly normal client (`redis-cli` does exactly that while you type),
        and parsing an empty buffer to find out there is nothing in it is work
        with a known answer.
        """
        while not self._closing:
            conn.busy = False
            data = await reader.read(READ_CHUNK)
            if not data:
                break  # peer closed
            # Busy from here until the replies are flushed: everything below owes
            # the client an answer, and shutdown must not cut it off mid-command.
            conn.busy = True
            conn.buf += data

            # Drain every command already buffered before touching the socket
            # again — this is what makes pipelining fast, and it falls out for
            # free from V1's buffer-oriented parser.
            while True:
                try:
                    command = resp.parse_command(conn.buf, self.settings.max_request_bytes)
                except ProtocolError as exc:
                    # Unrecoverable framing: there is no way to find the next
                    # frame boundary, so reply and close rather than
                    # resynchronizing on garbage. A *command*-level error keeps
                    # the connection (see `dispatch`); a *framing* error cannot.
                    resp.encode(Error(exc.resp_error()), conn.out)
                    await _flush(conn, writer)
                    return
                if command is None:
                    break  # partial frame — need more bytes
                reply = await dispatch(self.engine, self.settings, conn, command)
                resp.encode(reply, conn.out)

            await _flush(conn, writer)


async def dispatch(engine: Engine, settings: Settings, conn: Connection, command: Command) -> Reply:
    """Map one parsed command to a reply.

    An engine error becomes a `-ERR …` line rather than dropping the connection
    — that is the horizontal item about protocol errors not being connection
    errors, and it matters because a client that gets hung up on has to
    reconnect and replay, turning one bad command into an outage.

    The command table is deliberately small. Growing it (`INCR`, `EXPIRE`,
    `MGET`, `TTL`, the type commands) is exactly the surface a redis-compatible
    server grows, and it is ongoing work rather than a vertical — each one is a
    few lines here plus a decision about what it means on an LSM.
    """
    if not command:
        return Error("ERR empty command")

    # Command names are ASCII by definition; keys and values stay raw bytes and
    # are never decoded — that is the binary-safety criterion, enforced by
    # simply not having a `.decode()` anywhere near them.
    name = command[0].decode("ascii", "replace").upper()

    if not conn.authenticated and name not in NO_AUTH_REQUIRED:
        return Error(NoAuth().resp_error())

    match name:
        case "PING":
            match len(command):
                case 1:
                    return "PONG"
                case 2:
                    return command[1]
                case _:
                    return _arity("ping")

        case "ECHO":
            return command[1] if len(command) == 2 else _arity("echo")

        case "AUTH":
            # `AUTH <password>` or, since redis 6, `AUTH <user> <password>`.
            match len(command):
                case 2:
                    supplied = command[1]
                case 3:
                    supplied = command[2]
                case _:
                    return _arity("auth")
            if not settings.auth_required:
                return Error("ERR Client sent AUTH, but no password is set.")
            # `secrets.compare_digest`, not `==`: a plain comparison returns as
            # soon as two bytes differ, so its runtime leaks how many leading
            # characters were right. That is a practical attack over a LAN, and
            # the constant-time version costs nothing here.
            if secrets.compare_digest(supplied, settings.requirepass.encode()):
                conn.authenticated = True
                return "OK"
            return Error(WrongPass().resp_error())

        case "SET":
            if len(command) != 3:
                return _arity("set")
            return await _guard(engine.set(command[1], command[2]), then="OK")

        case "GET":
            if len(command) != 2:
                return _arity("get")
            try:
                return await engine.get(command[1])
            except AppError as exc:
                return Error(exc.resp_error())

        case "DEL":
            if len(command) < 2:
                return _arity("del")
            removed = 0
            for key in command[1:]:
                try:
                    removed += 1 if await engine.delete(key) else 0
                except AppError as exc:
                    return Error(exc.resp_error())
            return removed

        case "EXISTS":
            if len(command) < 2:
                return _arity("exists")
            present = 0
            for key in command[1:]:
                try:
                    present += 1 if await engine.get(key) is not None else 0
                except AppError as exc:
                    return Error(exc.resp_error())
            return present

        case "DBSIZE":
            # Partial (active memtable only) until the read path reconciles
            # levels — enough to answer `redis-cli` on the scaffold. Making it
            # exact once V4/V6 land is genuinely hard: an exact count across
            # overlapping SSTables means a full merge, which is why real redis
            # keeps a running counter instead of asking the store.
            return engine.stats().keys_memtable

        case "COMMAND":
            # `redis-cli` probes COMMAND / COMMAND DOCS on connect. An empty
            # array is a harmless "no introspection to offer" that keeps the CLI
            # happy instead of printing an error before your first command.
            return []

        case "QUIT":
            return "OK"

        case other:
            return Error(f"ERR unknown command '{other}'")


async def _guard(call: Coroutine[Any, Any, None], *, then: str) -> Reply:
    """Await an engine call that returns nothing, mapping failure to a RESP
    error line and success to a fixed status."""
    try:
        await call
    except AppError as exc:
        return Error(exc.resp_error())
    return then


def _arity(command: str) -> Error:
    return Error(f"ERR wrong number of arguments for '{command}' command")


async def _flush(conn: Connection, writer: asyncio.StreamWriter) -> None:
    """Write the batched replies and apply backpressure. See the module
    docstring on why `drain()` is not optional."""
    if not conn.out:
        return
    writer.write(conn.out)
    conn.out.clear()
    await writer.drain()


def _peer_name(writer: asyncio.StreamWriter) -> str:
    """`host:port` of the peer, for log lines.

    `get_extra_info` is typed `Any` because what it returns depends on the
    transport — an IPv4 peer is a 2-tuple, IPv6 a 4-tuple, a Unix socket a
    string. Formatting whatever came back keeps this honest under every
    transport without pretending to know which one you are on.
    """
    # `get_extra_info` is untyped, so the cast is where the unknown stops. It is
    # a claim about asyncio's transports, not a silenced error: an IP peer is a
    # tuple, a Unix-socket peer is a path, and anything else is not something to
    # guess about.
    peer = cast("tuple[object, ...] | str | None", writer.get_extra_info("peername"))
    if isinstance(peer, tuple):
        return ":".join(str(part) for part in peer[:2])
    return peer if isinstance(peer, str) else "unknown"
