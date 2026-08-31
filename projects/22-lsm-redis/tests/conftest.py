"""Shared fixtures.

The acceptance tests for V1-V7 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness — and this project needs two of
them, because it has two wires.

**`client`** drives the HTTP sidecar over `httpx.ASGITransport` — the same ASGI
interface uvicorn uses, so the tests stay genuinely async (an `await` bug in your
code shows up as one) without `TestClient`'s sync-portal indirection.

**`resp`** is a real TCP client on a real socket, connected to the RESP server
the lifespan started. RESP cannot be tested in memory: framing, partial reads and
pipelining are properties *of the wire*, and an in-process fake has no wire. It
is also the only harness that can prove the thing V1 is actually graded on —
that a stock client works — because it sends the same bytes `redis-cli` does.

Note `tmp_path`: every test gets its own `data_dir`. The engine's entire state is
the filesystem, so a shared directory would let one test's SSTables become
another test's "recovered" store — and worse, two engines over one directory are
two writers on one WAL, which corrupts silently and surfaces three tests later as
a flake.

`RESP_PORT=0` for the same reason: the OS picks a free port, so tests never race
each other (or a real redis you left running) for 6379.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from lsm_redis.config import Settings
from lsm_redis.engine import Engine
from lsm_redis.main import create_app
from lsm_redis.server import RespServer

__all__ = ["RespClient"]


class RespClient:
    """A raw TCP client for the RESP port.

    Deliberately thin: it sends bytes you choose and returns bytes it got. It
    does *not* parse RESP, because parsing RESP is V1 — a test harness that
    decoded replies for you would be handing you half the vertical, and a test
    that asserts on exact bytes is a much better proof that a stock client will
    be happy than one that asserts through your own parser.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def read(self, timeout: float = 2.0) -> bytes:
        """Read whatever arrives next, or `b""` if the peer closed.

        Bounded by a timeout because the interesting failure — "the server never
        replied" — is indistinguishable from "the server has not replied *yet*"
        without one, and an un-timed read turns that into a hung test suite
        rather than a failing test.
        """
        try:
            async with asyncio.timeout(timeout):
                return await self.reader.read(65536)
        except TimeoutError:
            return b""

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """An engine over a throwaway data dir, on an ephemeral RESP port.

    `memtable_max_bytes` is tiny on purpose: V3 grades that the memtable
    *freezes* and V4 that it flushes, and at the 4 MiB default a test would have
    to write 4 MiB to see either happen once.
    """
    return Settings(
        resp_port=0,
        http_port=8080,
        data_dir=tmp_path / "data",
        memtable_max_bytes=4096,
        block_size_bytes=256,
        block_cache_bytes=64 * 1024,
        max_request_bytes=1024 * 1024,
    )


@pytest.fixture
def engine(settings: Settings) -> Engine:
    """A bare engine with no server in front of it.

    For unit tests of the storage layers, which is most of them — the verticals
    below V1 have nothing to do with sockets, and testing them through one would
    make every WAL test depend on a codec you have not written yet.
    """
    return Engine.open(settings)


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """The booted app: engine opened, RESP server listening, both torn down.

    Entering `lifespan_context` runs the real startup path, so a test can never
    pass against wiring that would fail under uvicorn — and exiting it runs the
    real shutdown path, which is how the graceful-shutdown criterion gets tested
    at all.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client against the sidecar."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://lsm") as http_client:
        yield http_client


@pytest.fixture
def resp_server(app: FastAPI) -> RespServer:
    """The RESP server the lifespan started, so a test can ask which port it
    actually bound."""
    server = getattr(app.state, "resp_server", None)
    assert isinstance(server, RespServer)
    return server


@pytest.fixture
async def resp(resp_server: RespServer) -> AsyncGenerator[RespClient]:
    """A TCP connection to the RESP port, closed with the test."""
    reader, writer = await asyncio.open_connection("127.0.0.1", resp_server.port)
    conn = RespClient(reader, writer)
    try:
        yield conn
    finally:
        await conn.close()
