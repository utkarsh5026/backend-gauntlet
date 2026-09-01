"""Shared fixtures.

The acceptance tests for V1-V6 are yours to write (see the SPEC's "Proof"
lines). What lives here is only the harness — and this project needs two of
them, because it has two wires.

**`http`** drives the control plane over `httpx.ASGITransport` — the same ASGI
interface uvicorn uses, so the tests stay genuinely async (an `await` bug in your
code shows up as one) without `TestClient`'s sync-portal indirection.

**`peer_client`** is a real TCP client on a real socket, connected to the peer
port the lifespan opened. The peer wire cannot be tested in memory: framing,
partial reads and the 68-byte handshake are properties *of the wire*, and an
in-process fake has no wire. It is also the only harness that can prove the
thing V4 is graded on — that a strict reference client like `transmission` stays
connected — because it sends the same bytes one does.

Note `tmp_path`: every test gets its own `download_dir`. A store's whole state is
the filesystem, so a shared directory would let one test's pieces become
another's "resumed" download — and the failure would surface three tests later,
looking like a flake.

`PEER_PORT=0` for the same reason: the OS picks a free port, so tests never race
each other (or a real client you left running) for 6819.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bittorrent.client import Client
from bittorrent.config import Settings
from bittorrent.main import create_app
from bittorrent.seeder import Seeder
from bittorrent.state import AppState

__all__ = ["PeerClient"]


class PeerClient:
    """A raw TCP client for the peer port.

    Deliberately thin: it sends bytes you choose and returns bytes it got. It
    does *not* build handshakes or parse messages, because building and parsing
    them is V4 — a harness that framed messages for you would be handing you half
    the vertical, and a test that asserts on exact bytes is a far better proof
    that `transmission` will be happy than one that asserts through your own
    encoder.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def read(self, timeout: float = 2.0) -> bytes:
        """Read whatever arrives next, or `b""` if nothing came.

        Bounded by a timeout because the interesting failure — "the seeder never
        answered" — is indistinguishable from "it has not answered *yet*" without
        one, and an un-timed read turns that into a hung suite rather than a
        failing test.

        A `ConnectionResetError` is also `b""`, and that is not papering over
        anything: closing a socket that still has *unread inbound* data makes the
        kernel send an RST rather than a FIN. On the scaffold the session raises
        before it reads your handshake, so those 68 bytes are sitting unread and
        a reset is the correct, expected outcome. Whether the read wins the race
        against the reset is a timing detail of the local network stack, so a
        harness that let it through would be a flake rather than a signal —
        "the peer hung up without answering" is one observation either way.
        """
        try:
            async with asyncio.timeout(timeout):
                return await self.reader.read(65536)
        except (TimeoutError, ConnectionResetError):
            return b""

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A client over a throwaway download dir, on an ephemeral peer port.

    `upload_slots` is small on purpose: V6 grades that the cap *holds*, and
    proving "at most K unchoked" needs more leechers than slots — which is much
    cheaper to arrange with K=2 than with the default 4.
    """
    return Settings(
        port=8080,
        peer_port=0,
        download_dir=tmp_path / "data",
        disk_workers=2,
        max_peers=8,
        upload_slots=2,
        pipeline_depth=4,
        run_seeder=False,
    )


@pytest.fixture
def seeding_settings(settings: Settings) -> Settings:
    """The same client with the inbound listener switched on."""
    return settings.model_copy(update={"run_seeder": True})


@pytest.fixture
def engine(settings: Settings) -> Client:
    """A bare client with no server in front of it.

    For unit tests of the registry and identity, which have nothing to do with
    sockets — testing them through one would make every peer-id test depend on a
    listener.
    """
    return Client(settings)


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    """The booted app: client built, both teardowns run.

    Entering `lifespan_context` runs the real startup path, so a test can never
    pass against wiring that would fail under uvicorn — and exiting it runs the
    real shutdown path, which is how the graceful-shutdown criterion gets tested
    at all.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def seeding_app(seeding_settings: Settings) -> AsyncGenerator[FastAPI]:
    """The app with the seeder listening, for tests that need the peer port."""
    application = create_app(seeding_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def http(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An HTTP client against the control plane."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bt") as client:
        yield client


@pytest.fixture
def seeder(seeding_app: FastAPI) -> Seeder:
    """The seeder the lifespan started, so a test can ask which port it actually
    bound."""
    state = getattr(seeding_app.state, "app_state", None)
    assert isinstance(state, AppState)
    assert isinstance(state.seeder, Seeder)
    return state.seeder


@pytest.fixture
async def peer_client(seeder: Seeder) -> AsyncGenerator[PeerClient]:
    """A TCP connection to the peer port, closed with the test."""
    reader, writer = await asyncio.open_connection("127.0.0.1", seeder.port)
    conn = PeerClient(reader, writer)
    try:
        yield conn
    finally:
        await conn.close()
