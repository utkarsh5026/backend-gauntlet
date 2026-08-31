"""V1 — The reverse-proxy forwarding core.

Module: `src/api_gateway/proxy.py`. This is the byte path: take an inbound
request, rewrite it for the chosen backend, send it upstream over the pooled
client, and hand the response back to the client — without ever holding a whole
body in memory, and without leaking headers that belong to one TCP connection
onto a different one.

"Just forward it" is four separate problems wearing a trench coat:

1. **Streaming.** A 2 GiB upload must cost kilobytes of RSS, not gigabytes.
2. **Hop-by-hop hygiene.** `Connection`, `Keep-Alive`, `TE`, `Trailer`,
   `Transfer-Encoding`, `Upgrade` and `Proxy-*` describe *this* hop and must die
   here — in both directions (RFC 7230 §6.1).
3. **Provenance.** The backend can no longer see the client, so the proxy has to
   tell it who called and how: `X-Forwarded-For`, `X-Forwarded-Proto`, `Via`.
4. **Connection reuse.** Without keep-alive you pay a TCP (and upstream TLS)
   handshake on every single request.

## The Python that makes or breaks this

**Streaming in.** `request.stream()` is an async iterator of `bytes` chunks, and
httpx accepts an async iterable as `content=`. Hand one to the other and the body
flows through you a chunk at a time. The failure mode is `await request.body()`,
which reads the whole thing into a `bytes` object first — that single call is the
difference between a proxy and a buffer.

**Streaming out.** Get the response with `stream=True`:

    upstream_req = client.build_request(method, url, headers=..., content=...)
    upstream = await client.send(upstream_req, stream=True)

then wrap `upstream.aiter_raw()` in a `StreamingResponse`. Two details, both of
which are silent when wrong:

* **`aiter_raw()`, never `aiter_bytes()`.** `aiter_bytes` transparently decodes
  `Content-Encoding`, so a gzipped upstream response arrives at the client
  decompressed while still carrying `Content-Encoding: gzip` and the compressed
  `Content-Length`. The client then fails to decode a body that is already plain.
  A proxy relays bytes; it does not have opinions about them.
* **The response must be closed, or the connection never goes back to the pool.**
  With `stream=True` you own `upstream.aclose()`. Since the body outlives your
  function, attach it as `StreamingResponse(..., background=BackgroundTask(
  upstream.aclose))` so it runs when the last byte has gone out. Skip this and
  every request leaks a connection — the pool quietly stops being a pool, and the
  keep-alive criterion fails while everything still *looks* fine.

**Two timeouts, not one.** The client's `httpx.Timeout` bounds connect and
per-read idle time. The overall deadline is `async with asyncio.timeout(...)`.
Where you put that boundary is a real decision: wrap only the `send` and you are
bounding *time to response head*, which is what you want, because a legitimate
slow download must not be killed for being large. Wrap the whole body relay and a
2 GiB transfer over a slow link becomes a 504. Bound the head with the deadline,
bound the body with a read-idle timeout, and write down in `docs/10-design.md`
which is which.

**Order your `except` clauses.** `httpx.ConnectTimeout` inherits from *both*
`TimeoutException` and `TransportError`. Catch `TransportError` first and every
connect timeout silently becomes a 502 that should have been a 504 — the two
codes mean different things to a retry layer, so the bug is invisible until
someone's retries amplify against a slow backend.

**The request body is unmeasured.** `Content-Length` is a claim, and a chunked
request need not send one. So `MAX_BODY_BYTES` has to be enforced *while*
streaming — count bytes as they pass and abort past the cap. Note the ugly part:
once you have started relaying upstream you can no longer answer 413 cleanly, so
the cheap `Content-Length` check up front is worth doing too, precisely because
it catches the honest oversized request before anything is committed.
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import Response

from .balancer import Backend
from .errors import AppError

__all__ = ["HOP_BY_HOP_HEADERS", "forward"]

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
"""Headers that belong to a single hop and must never be forwarded (RFC 7230 §6.1).

Lowercase because HTTP header names are case-insensitive and both Starlette and
httpx normalize to lowercase — comparing anything else is a bug waiting for a
client that sends `Proxy-Authorization` with different capitalization.

**This set is not the whole rule.** `Connection` also *names* other headers that
are hop-by-hop for this connection only (`Connection: keep-alive, X-Hop-Token`
makes `X-Hop-Token` hop-by-hop too). A correct implementation unions this set with
whatever the inbound `Connection` header lists — in both directions, since an
upstream can send one back.
"""


async def forward(
    client: httpx.AsyncClient,
    backend: Backend,
    request: Request,
    *,
    deadline: float,
    max_body_bytes: int,
) -> Response:
    """Forward `request` to `backend` and stream the response back to the client.

    `client` is the process-wide pooled `httpx.AsyncClient` built in `main.py` —
    take it as an argument and never build one here. A client constructed
    per-request is a connection pool with one member and a lifetime of one
    request, which is the same as having no pool at all, and it is the single most
    common way the keep-alive criterion is failed.

    TODO(V1): the forwarding core.
     1. Build the upstream URL from `backend.addr` plus the request's path and
        **raw** query string. Take them from `request.url.path` and
        `request.scope["query_string"]` — not from a FastAPI path parameter,
        which arrives percent-*decoded*. Re-encoding a decoded path is lossy:
        `/a%2Fb` and `/a/b` are different resources, and the round trip silently
        merges them.
     2. Copy the request headers, dropping every hop-by-hop one (see
        `HOP_BY_HOP_HEADERS`, plus anything the inbound `Connection` names). Drop
        `Content-Length` and `Transfer-Encoding` as well and let httpx frame the
        body it is actually sending — a copied length that disagrees with the
        bytes on the wire is a request-smuggling primitive, not a rounding error.
     3. Set provenance: **append** the peer (`request.client.host`) to any
        existing `X-Forwarded-For` rather than trusting what arrived — an inbound
        value is attacker-controlled, and a backend doing ACLs or rate limits on
        it is spoofable the moment you pass it through untouched. Set
        `X-Forwarded-Proto` from `request.url.scheme`, decide what happens to
        `Host` (rewrite it to the upstream authority, or preserve the original in
        `X-Forwarded-Host` — either is fine, *document which*), and add a `Via`.
     4. Stream the body upstream (`content=request.stream()`), enforcing
        `max_body_bytes` as the chunks pass, and bound the wait for the response
        head with `deadline`.
     5. Return a `StreamingResponse` over `upstream.aiter_raw()`, carrying the
        upstream status and its non-hop-by-hop headers, and closing the upstream
        response in a background task.
     6. Map failures: a refused/reset connection or DNS failure -> `BadGateway`;
        a deadline or read timeout -> `GatewayTimeout`; a body past the cap ->
        `PayloadTooLarge`. Never let an `httpx` exception escape as a 500, never
        hang, never panic.

    TODO(V3/V4): while you are in here, wire the accounting the other verticals
    read — `backend.in_flight` up before dispatch and down in a `finally`,
    `backend.ewma_seconds` updated from the observed latency, and
    `backend.circuit.record_success()` / `record_failure()` on the way out. The
    balancer and the breaker are both blind until this happens, which is a
    confusing way to discover that least-connections "doesn't work".

    Note the `finally`: a client that disconnects mid-request cancels this
    coroutine, and a decrement that only runs on the happy path leaks the counter
    upward permanently. See `balancer.py`.
    """
    raise NotImplementedError(
        "V1: rewrite -> strip hop-by-hop -> stream upstream -> stream the response back"
    )


def upstream_error(exc: Exception) -> AppError:
    """Map an httpx transport failure to the right gateway status.

    A separate function because you will want it in the health checker (V4) too,
    and because the mapping is a decision worth being able to point at rather than
    something buried in an `except` ladder.

    TODO(V1): `httpx.TimeoutException` -> `GatewayTimeout` (504),
    `httpx.TransportError` -> `BadGateway` (502) — **in that order**, since
    `ConnectTimeout` is both. Anything else is genuinely unexpected and should
    surface as a 500 rather than being quietly relabelled a backend problem.
    """
    raise NotImplementedError("V1: map httpx transport failures to 502 / 504")
