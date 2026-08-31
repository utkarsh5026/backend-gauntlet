"""Shared fixtures.

Two harnesses live here, for the two ways this project has to be tested.

**`client`** drives the whole gateway app over `httpx.ASGITransport` — the same
ASGI interface uvicorn uses, so the tests stay genuinely async (an `await` bug in
your code shows up as one) without `TestClient`'s sync-portal indirection. This is
how the wiring is tested.

**`EchoBackend`** is a *real* HTTP server on a *real* socket. A proxy cannot be
honestly tested in memory: the things V1 is graded on — did the backend receive
this header, how many TCP connections were opened, did the response head arrive
before the body finished — are properties of the wire, and an in-process fake has
no wire. So the acceptance suite stands up an actual uvicorn server on an
ephemeral port and asks it what it saw.

The acceptance tests for V2-V4 are yours to write (see the SPEC's "Proof" lines).
V1's are provided in `test_proxy_acceptance.py` because they are black-box — they
assert only on observable behaviour and never on how `forward` is written, so they
cannot hand you the implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.types import Message, Receive, Scope, Send

from api_gateway.config import Settings
from api_gateway.main import create_app

__all__ = ["EchoBackend", "Seen", "body_digest", "drain"]


def body_digest(data: bytes) -> str:
    """A short digest of a body, so a multi-MiB payload is verified without
    keeping a second copy of it around to compare against."""
    return hashlib.blake2b(data, digest_size=16).hexdigest()


async def drain(response: Any) -> tuple[int, dict[str, str], bytes]:
    """Run a response through the ASGI protocol and collect it.

    Not `response.body`: a streaming response has no `.body`, and reaching for one
    would quietly pass only for an implementation that buffered. Running it the way
    a server does also runs its `background` task, which is where the upstream
    response gets closed — and therefore where the connection goes back to the
    pool. That matters for the keep-alive test below.

    **Two details that are load-bearing, both learned the hard way.** The scope
    declares `spec_version` 2.4, and `receive` never returns. Below spec version
    2.4, `StreamingResponse` races `stream_response` against a
    `listen_for_disconnect(receive)` and cancels the first when the second returns
    — so a `receive` that hands back `http.disconnect` immediately cancels the
    body before a single chunk is sent, and every streaming assertion here fails
    against a *correct* implementation. A real server holds the connection open
    until the client actually goes away; this does the same.
    """
    status = 0
    headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
            for key, value in message.get("headers", []):
                headers[bytes(key).decode("latin-1").lower()] = bytes(value).decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    never = asyncio.Event()

    async def receive() -> Message:
        await never.wait()
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
    }
    await response(scope, receive, send)
    return status, headers, b"".join(chunks)


@dataclass(slots=True)
class Seen:
    """One request as the *backend* saw it — ground truth for "what did the proxy
    actually put on the wire?"."""

    method: str
    path: str
    query: str
    """Raw query string, so percent-encoding damage shows up."""
    headers: dict[str, str]
    body_len: int
    body_digest: str
    peer: tuple[str, int]
    """The client-side socket address. A distinct peer *port* means a distinct TCP
    connection — that is how the keep-alive test counts connections."""
    head_at: float
    """When the request head arrived, before the body was read. A proxy that
    buffers the request body delays this; a streaming one does not."""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(slots=True)
class EchoBackend:
    """A real HTTP server that remembers exactly what it was sent.

    Not named `TestBackend`: pytest tries to collect any class called `Test*` and
    then warns that it cannot, because this one has a constructor. The warning is
    harmless and the rename is free.
    """

    host: str
    port: int
    requests: list[Seen] = field(default_factory=list[Seen])

    @property
    def addr(self) -> str:
        """`host:port` — what a `Backend` points at."""
        return f"{self.host}:{self.port}"

    def only_request(self) -> Seen:
        """The single request this backend saw. Fails loudly if the proxy never
        reached it, which is the more common outcome while V1 is unbuilt."""
        assert len(self.requests) == 1, (
            f"expected the backend to receive exactly 1 request, it saw {len(self.requests)}"
        )
        return self.requests[0]

    def connections_opened(self) -> int:
        """How many distinct TCP connections the proxy opened to this backend."""
        return len({seen.peer for seen in self.requests})


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(bytes(message.get("body", b"")))
        more = bool(message.get("more_body", False))
    return b"".join(chunks)


async def _send_response(
    send: Send,
    status: int,
    headers: Iterable[tuple[bytes, bytes]],
    body: bytes,
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": list(headers)})
    await send({"type": "http.response.body", "body": body})


def _make_app(recorder: EchoBackend) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """A raw ASGI app — no framework.

    Deliberately hand-rolled: a framework would normalize headers, re-encode the
    path and hide the raw query string, and those are exactly the things the
    acceptance tests need to observe untouched.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        path: str = scope["path"]

        if path == "/slow-body":
            # Three chunks, 300ms apart: the body completes at ~900ms. A proxy that
            # buffers the upstream body cannot return its head before then.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            for chunk in (b"first-", b"second-", b"third"):
                await asyncio.sleep(0.3)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        if path == "/never-responds":
            # Accepts the connection, then never answers — the classic slow upstream.
            await asyncio.sleep(60)
            await _send_response(send, 200, [], b"")
            return

        if path == "/hop-by-hop-response":
            # A backend that (rudely) sets hop-by-hop headers on its *response*.
            # None of them belong to the gateway->client connection.
            await _send_response(
                send,
                200,
                [
                    (b"connection", b"keep-alive, x-secret-hop"),
                    (b"x-secret-hop", b"must-not-leak"),
                    (b"keep-alive", b"timeout=5"),
                    (b"proxy-authenticate", b'Basic realm="upstream"'),
                    (b"x-backend-note", b"end-to-end-header"),
                ],
                b"upstream body",
            )
            return

        # Catch-all: record everything, then echo the body back so the response
        # direction is checkable too.
        head_at = time.perf_counter()
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        body = await _read_body(receive)
        client: tuple[str, int] = scope.get("client") or ("?", 0)

        recorder.requests.append(
            Seen(
                method=scope["method"],
                path=path,
                query=bytes(scope.get("query_string", b"")).decode("latin-1"),
                headers=headers,
                body_len=len(body),
                body_digest=body_digest(body),
                peer=client,
                head_at=head_at,
            )
        )

        # `x-test-status` lets a test choose the upstream's status code, so status
        # passthrough is checkable without a second handler.
        status = int(headers.get("x-test-status", "200"))
        await _send_response(
            send,
            status,
            [
                (b"x-backend-note", b"handled-by-backend"),
                (b"content-type", b"application/octet-stream"),
            ],
            body,
        )

    return app


@pytest.fixture
async def upstream() -> AsyncGenerator[EchoBackend]:
    """A real backend on an ephemeral port, torn down with the test.

    The socket is bound here rather than by uvicorn so the port is known before
    the server starts — no polling for "which port did it pick?".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()

    backend = EchoBackend(host=host, port=port)
    config = uvicorn.Config(
        _make_app(backend),
        log_level="critical",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # pragma: no branch - startup is immediate
        await asyncio.sleep(0.01)

    try:
        yield backend
    finally:
        server.should_exit = True
        await serving
        sock.close()


@pytest.fixture
def settings() -> Settings:
    """Gateway settings with timings tightened well below the defaults, so a test
    that waits for a deadline waits milliseconds rather than ten seconds."""
    return Settings(
        port=8080,
        upstream_backends="127.0.0.1:9010",
        upstream_connect_timeout_ms=500,
        request_timeout_ms=2_000,
        health_probe_ms=200,
        circuit_failure_threshold=3,
        circuit_open_cooldown_ms=500,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """A booted gateway.

    Entering `lifespan_context` runs the real startup path — the pooled client is
    built, the route table is compiled, the health checker is spawned, and
    everything is torn down on the way out — so a test can never pass against
    wiring that would fail in production.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            yield http
