"""V1 acceptance tests — the reverse-proxy forwarding core (`src/api_gateway/proxy.py`).

These are **black-box** tests: they stand up a real HTTP backend on a real socket,
hand `proxy.forward` a real request, and assert only on what is *observable* —
what the backend received, what the client got back, how many TCP connections were
opened, and how long things took. They never look at how `forward` is written, so
they cannot hand you the implementation.

Each test maps to one bullet of SPEC.md V1 "Done when ALL true"; the test name says
which. Run them with:

```bash
make test
uv run pytest tests/test_proxy_acceptance.py -q
uv run pytest tests/test_proxy_acceptance.py -x -vv   # first failure, full messages
```

Until `forward` exists every one of them raises `NotImplementedError` — that's the
worklist, in priority order.

**About the `xfail` marker below.** These tests were written before the code they
test, so on a fresh scaffold they are all red, and a permanently-red suite is a
suite people stop reading. The module-level marker says "expected to fail, *and
only* by raising `NotImplementedError`". That one qualifier is what keeps it
honest: the moment `forward` does anything at all, any assertion it gets wrong is
a hard failure, not an expected one. So `make verify` is green on the scaffold,
green when V1 is right, and red the whole time V1 is wrong — which is exactly when
you want it red. When every test here reports XPASS, delete the marker.

**Scope note:** this file exercises V1 *only*. It builds a `Backend` by hand and
calls `forward` directly, so it never touches `Router.match_request` (V2),
`Balancer.pick` (V3), or `CircuitBreaker.allow` (V4) — all still unimplemented. If
you wire circuit-breaker accounting (`backend.circuit.record_success()`) into
`forward` before building V4, these tests will fail inside V4's
`NotImplementedError` instead. Add that accounting when you get to V4.

**Why a hand-built `Request` and not the whole app.** A `Request` is a `scope`
dict plus a `receive` callable, and building one by hand is both how you keep this
suite scoped to V1 and a useful thing to have done once: `scope` is the entire
contract between a server and an ASGI app, and half of V1 is about handling the
parts of it — `raw_path`, `query_string`, `client` — that a framework normally
hides from you.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import Request
from starlette.types import Message

from api_gateway.balancer import Backend
from api_gateway.config import Settings
from api_gateway.errors import AppError, BadGateway, GatewayTimeout
from api_gateway.main import upstream_client
from api_gateway.proxy import forward as _forward
from tests.conftest import EchoBackend, body_digest, drain

pytestmark = pytest.mark.xfail(
    raises=NotImplementedError,
    reason="V1 (proxy.forward) is not built yet — these tests are the worklist",
    strict=False,
)

CONNECT_TIMEOUT = 0.5
"""Upstream connect timeout for the test client — short, so the 502 test is quick."""

REQUEST_DEADLINE = 2.0
"""The per-request deadline `forward` is expected to enforce.

It has to clear the slowest *legitimate* body in this suite (~900 ms) while still
tripping well inside the 504 test's 5-second safety net. A deadline that also has
to cover a slow-but-honest download is the tension the real value lives in."""

MAX_BODY_BYTES = 32 * 1024 * 1024
"""Comfortably above the 8 MiB upload below: this suite tests streaming, not the
body cap, and a cap that fires here would only obscure that."""


def client() -> httpx.AsyncClient:
    """One pooled client, built the way production builds it."""
    return upstream_client(
        Settings(
            upstream_connect_timeout_ms=int(CONNECT_TIMEOUT * 1000),
            request_timeout_ms=int(REQUEST_DEADLINE * 1000),
        )
    )


async def forward(http: httpx.AsyncClient, backend: Backend, request: Request) -> Any:
    """The single place this suite calls into V1.

    If `forward`'s signature changes as you build it, update **this one function**
    and the whole suite keeps working.
    """
    return await _forward(
        http,
        backend,
        request,
        deadline=REQUEST_DEADLINE,
        max_body_bytes=MAX_BODY_BYTES,
    )


# ---------------------------------------------------------------------------
# Building an inbound request the way a server hands one to the gateway
# ---------------------------------------------------------------------------


def inbound(
    method: str,
    target: str,
    headers: dict[str, str] | None = None,
    body: bytes | AsyncIterator[bytes] | None = None,
) -> Request:
    """An inbound request in origin form (`/path?query`, no scheme or authority)
    plus a `Host` header — exactly the shape uvicorn produces."""
    path, _, query = target.partition("?")
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", b"gateway.test")]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        # The undecoded path. V1 must forward this, not the decoded `path`.
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("203.0.113.7", 54321),
        "server": ("gateway.test", 8080),
    }
    return Request(scope, _receiver(body))


def _receiver(body: bytes | AsyncIterator[bytes] | None) -> Callable[[], Awaitable[Message]]:
    """Turn a body into the ASGI `receive` callable a `Request` reads from."""
    if body is None:
        body = b""

    if isinstance(body, bytes):
        sent = False

        async def receive_once() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive_once

    chunks = body
    done = False

    async def receive_stream() -> Message:
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        try:
            chunk = await anext(chunks)
        except StopAsyncIteration:
            done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    return receive_stream


async def expect_response(coro: Awaitable[Any]) -> Any:
    """`forward` returned an error where a response was expected — say which."""
    try:
        return await coro
    except GatewayTimeout as exc:
        pytest.fail(
            "`forward` timed out against a *healthy* backend. Your per-request "
            f"deadline is tighter than a legitimate slow body: this suite's slowest "
            f"honest response takes ~900ms and REQUEST_DEADLINE is {REQUEST_DEADLINE}s. "
            f"A deadline that kills real downloads is a bug, not a safety net. ({exc})"
        )
    except AppError as exc:
        pytest.fail(
            f"`forward` returned an error against a healthy backend: {exc}\n"
            "Hint: a `Transfer-Encoding` or `Content-Length` header copied from the "
            "inbound request will make httpx frame the upstream body wrongly — those "
            "are hop-by-hop / recomputed and must not be forwarded verbatim."
        )


# ---------------------------------------------------------------------------
# V1 · "Method, path, query, response status and headers are preserved end to end"
# ---------------------------------------------------------------------------


async def test_method_path_query_and_status_survive_the_hop(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    request = inbound(
        "POST",
        "/api/v2/orders/42?q=two%20words&flag&n=7",
        headers={"x-test-status": "418", "content-type": "application/json"},
        body=b'{"hello":"world"}',
    )

    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        status, headers, body = await drain(response)

    assert status == 418, "the upstream's status code must reach the client unchanged"
    assert headers.get("x-backend-note") == "handled-by-backend", (
        "end-to-end response headers from the upstream must reach the client"
    )
    assert body == b'{"hello":"world"}', (
        "the body must round-trip unchanged (the backend echoes what it received)"
    )

    seen = upstream.only_request()
    assert seen.method == "POST", "the method must be preserved"
    assert seen.path == "/api/v2/orders/42", "the path must be preserved verbatim"
    assert seen.query == "q=two%20words&flag&n=7", (
        "the query string must be preserved *raw* — no re-encoding, no dropped keys. "
        "Take it from `request.scope['query_string']`, not from a parsed dict."
    )
    assert seen.header("content-type") == "application/json", (
        "end-to-end request headers must reach the upstream"
    )


# ---------------------------------------------------------------------------
# V1 · "Hop-by-hop headers are stripped between hops" (request direction)
# ---------------------------------------------------------------------------


async def test_hop_by_hop_request_headers_never_reach_the_backend(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    request = inbound(
        "GET",
        "/hop-check",
        headers={
            # `x-hop-token` is hop-by-hop *because this connection says so* —
            # RFC 7230 §6.1: anything listed in `Connection` dies at this hop too.
            "connection": "keep-alive, x-hop-token",
            "x-hop-token": "must-not-leak",
            "keep-alive": "timeout=5, max=1000",
            "te": "trailers",
            "trailer": "x-checksum",
            "upgrade": "websocket",
            "proxy-authorization": "Basic c2VjcmV0",
            "proxy-connection": "keep-alive",
            # ...and two end-to-end headers that must survive the trip.
            "x-request-id": "req-123",
            "authorization": "Bearer app-token",
        },
    )

    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        await drain(response)

    seen = upstream.only_request()
    for hop in (
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
        "x-hop-token",
    ):
        assert seen.header(hop) is None, (
            f"the backend received hop-by-hop header `{hop}: {seen.header(hop)}` — "
            "hop-by-hop headers belong to a single connection and must not be "
            "forwarded (`x-hop-token` counts: it was named in the inbound "
            "`Connection` header)"
        )

    assert seen.header("x-request-id") == "req-123", (
        "end-to-end headers must NOT be stripped — only hop-by-hop ones"
    )
    assert seen.header("authorization") == "Bearer app-token", (
        "end-to-end headers must NOT be stripped — only hop-by-hop ones"
    )


# ---------------------------------------------------------------------------
# V1 · "Hop-by-hop headers are stripped between hops" (response direction)
# ---------------------------------------------------------------------------


async def test_hop_by_hop_response_headers_never_reach_the_client(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    request = inbound("GET", "/hop-by-hop-response")

    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        _, headers, body = await drain(response)

    for hop in ("keep-alive", "proxy-authenticate", "x-secret-hop", "connection"):
        assert hop not in headers, (
            f"the client received hop-by-hop response header `{hop}` — stripping runs "
            "in *both* directions, and `x-secret-hop` was named in the upstream's "
            "`Connection` header"
        )
    assert headers.get("x-backend-note") == "end-to-end-header", (
        "end-to-end response headers must still pass through"
    )
    assert body == b"upstream body"


# ---------------------------------------------------------------------------
# V1 · "The proxy sets provenance headers"
# ---------------------------------------------------------------------------


async def test_proxy_stamps_its_own_provenance_headers(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    request = inbound("GET", "/who-sent-me")

    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        await drain(response)

    seen = upstream.only_request()
    assert seen.header("x-forwarded-for") is not None, (
        "the backend must be told who the original client was — set `X-Forwarded-For`"
    )
    assert seen.header("x-forwarded-proto") == "http", (
        "the backend can't see the client's scheme; the proxy must state it in "
        "`X-Forwarded-Proto` (this hop is plain HTTP)"
    )
    assert seen.header("via"), (
        "a proxy must announce itself in `Via` (RFC 7230 §5.7.1) so a hop chain is traceable"
    )

    # The inbound `Host: gateway.test` names the *gateway*, not the upstream. Send
    # it on unchanged and a backend doing host-based vhosting sees the wrong site.
    # If you deliberately pass the original Host through (some gateways do), keep it
    # recoverable in `X-Forwarded-Host` — either policy satisfies this assertion.
    host = seen.header("host") or ""
    assert host == upstream.addr or seen.header("x-forwarded-host") == "gateway.test", (
        f"the backend saw `Host: {host}` and no `X-Forwarded-Host`. Either rewrite "
        f"`Host` to the upstream authority (`{upstream.addr}`) or preserve the "
        "original in `X-Forwarded-Host` — pick one and document it."
    )


async def test_client_supplied_x_forwarded_for_is_not_blindly_trusted(
    upstream: EchoBackend,
) -> None:
    backend = Backend(upstream.addr)
    request = inbound("GET", "/spoof-check", headers={"x-forwarded-for": "10.0.0.1"})

    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        await drain(response)

    seen = upstream.only_request()
    xff = seen.header("x-forwarded-for")
    assert xff is not None, "`X-Forwarded-For` must be present"
    assert xff != "10.0.0.1", (
        "the client's `X-Forwarded-For: 10.0.0.1` reached the backend untouched — "
        "anything a client sends is attacker-controlled, so a backend that trusts "
        "XFF for rate limiting or ACLs is now spoofable. Append this hop's view of "
        "the peer (`10.0.0.1, 203.0.113.7`) or replace the header outright; "
        "document which."
    )


# ---------------------------------------------------------------------------
# V1 · "A request/response body is streamed — memory stays bounded"
# ---------------------------------------------------------------------------


async def test_large_body_round_trips_intact(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    # 8 MiB of non-repeating bytes: a chunking bug that drops, duplicates or
    # reorders a chunk changes the digest.
    payload = bytes(i % 251 for i in range(8 * 1024 * 1024))
    expected = body_digest(payload)

    request = inbound("POST", "/upload", body=payload)
    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        _, _, echoed = await drain(response)

    seen = upstream.only_request()
    assert seen.body_len == len(payload), (
        f"the upstream received {seen.body_len} of {len(payload)} bytes — "
        "the request body was truncated"
    )
    assert seen.body_digest == expected, (
        "the upstream received the right *number* of bytes but not the right bytes"
    )
    assert body_digest(echoed) == expected, "the response body came back corrupted"


async def test_response_body_is_streamed_not_buffered(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    # The backend emits 3 chunks 300ms apart, so the body completes at ~900ms.
    request = inbound("GET", "/slow-body")

    started = time.perf_counter()
    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        head_latency = time.perf_counter() - started
        _, _, body = await drain(response)
        total = time.perf_counter() - started

    assert head_latency < 0.25, (
        f"`forward` took {head_latency:.3f}s to return the response head from a "
        "backend whose body takes ~900ms to finish. That means the whole upstream "
        "body was collected before responding — which is exactly what makes a 1 GiB "
        "download cost 1 GiB of RSS. Send with `stream=True` and wrap "
        "`aiter_raw()` in a StreamingResponse instead of awaiting the body."
    )
    assert body == b"first-second-third", "streaming must not lose or reorder chunks"
    assert total >= 0.6, (
        "the full body arrived faster than the backend could have produced it — "
        "the test backend is not behaving as expected"
    )


async def test_request_body_is_streamed_not_buffered(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)

    async def slow_upload() -> AsyncIterator[bytes]:
        # First chunk ready immediately, second 600ms later. A streaming proxy
        # sends the *head* upstream at once; a buffering one can't send anything
        # until the last byte is in hand.
        yield b"part-1"
        await asyncio.sleep(0.6)
        yield b"part-2"

    request = inbound("POST", "/slow-upload", body=slow_upload())

    started = time.perf_counter()
    async with client() as http:
        response = await expect_response(forward(http, backend, request))
        await drain(response)

    seen = upstream.only_request()
    head_delay = seen.head_at - started
    assert head_delay < 0.4, (
        f"the upstream didn't see the request head until {head_delay:.3f}s in, but "
        "the body's first chunk was ready immediately. The inbound body was "
        "collected before the upstream request was sent — that's the 2 GiB upload "
        "buffering into RAM. Pass `content=request.stream()` to httpx; don't "
        "`await request.body()`."
    )
    assert seen.body_len == 12, "both chunks must arrive upstream (`part-1part-2`)"


# ---------------------------------------------------------------------------
# V1 · "Upstream connections are pooled/reused"
# ---------------------------------------------------------------------------


async def test_a_burst_of_requests_reuses_pooled_connections(upstream: EchoBackend) -> None:
    burst = 20
    backend = Backend(upstream.addr)

    # One client for the whole burst — the pool lives in the client, so a `forward`
    # that builds its own client per request throws the pool away every time.
    async with client() as http:
        for i in range(burst):
            request = inbound("GET", f"/burst/{i}")
            response = await expect_response(forward(http, backend, request))
            # Draining matters: the connection only returns to the pool once the
            # upstream response is closed, which happens in the response's
            # background task. Leak that and you leak the connection.
            await drain(response)

    assert len(upstream.requests) == burst, "every request in the burst must reach the backend"
    opened = upstream.connections_opened()
    assert opened <= 2, (
        f"{burst} sequential requests opened {opened} TCP connections. Keep-alive "
        "reuse should make that ~1: a fresh connection per request pays a handshake "
        "(and a TLS one upstream) on every call. Check that you use the client you "
        "were handed, don't send `Connection: close`, and close each upstream "
        "response so its connection is released."
    )


# ---------------------------------------------------------------------------
# V1 · "An unreachable or slow upstream yields a clean 502/504, never a panic
#       and never a hung request"
# ---------------------------------------------------------------------------


async def test_unreachable_upstream_is_a_502() -> None:
    import socket

    # Bind then close: the port is now reliably nobody's, so connect() is refused.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    sock.close()

    backend = Backend(f"127.0.0.1:{port}")
    request = inbound("GET", "/anything")

    async with client() as http:
        with pytest.raises(BadGateway):
            response = await forward(http, backend, request)
            await drain(response)


async def test_slow_upstream_is_a_504_not_a_hang(upstream: EchoBackend) -> None:
    backend = Backend(upstream.addr)
    request = inbound("GET", "/never-responds")

    # The outer timeout is the test's safety net, not the deadline under test: if
    # it fires, `forward` enforced no deadline of its own.
    guard = 5.0
    async with client() as http:
        try:
            async with asyncio.timeout(guard):
                with pytest.raises(GatewayTimeout):
                    response = await forward(http, backend, request)
                    await drain(response)
        except TimeoutError:
            pytest.fail(
                f"`forward` hung for {guard}s against a backend that accepts the "
                "connection and never answers. V1 requires an enforced per-request "
                "deadline: without one, a single slow upstream ties up a connection "
                "and a task per request until the gateway falls over. Bound the wait "
                "for the response head with `async with asyncio.timeout(deadline)` "
                "and raise `GatewayTimeout` when it expires."
            )
