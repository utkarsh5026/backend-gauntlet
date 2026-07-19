# The Proxy Byte Path — Streaming, Headers, and Connection Reuse

> **What this teaches:** what a reverse proxy actually does on the wire — and the
> three ways "just forward the request" goes wrong. No prior proxy knowledge assumed.
> This prepares you for **V1** in [SPEC.md](../SPEC.md): the `forward()` you'll build
> in [proxy.rs](../src/proxy.rs), using the pooled `UpstreamClient` and `AppError`
> already wired in [main.rs](../src/main.rs) and [error.rs](../src/error.rs).

---

## The one sentence to hold onto

**A proxy is not a pipe — it is two separate connections with a per-request
rewriter in between: bodies *stream* through it, hop-by-hop headers *stop* at it,
and connections *outlive* the requests they carry.**

---

## 1. What a reverse proxy actually is

When a client talks to a backend *through* your gateway, there is no single
connection from client to backend. There are **two independent TCP connections**,
and your gateway owns the middle:

```
   client                    gateway                     backend
     │                          │                           │
     │== TCP connection A ======│                           │
     │                          │====== TCP connection B ===│
     │                          │                           │
     │  GET /api/users ────────▶│                           │
     │                          │  (rewrite the request)    │
     │                          │  GET /api/users ─────────▶│
     │                          │◀───────── 200 OK          │
     │◀──────── 200 OK          │                           │
```

Everything interesting about V1 follows from that picture:

- Connection **A** and connection **B** have *different* properties (different
  keep-alive lifetimes, maybe different HTTP versions, different TLS). Headers
  that describe *a connection* therefore cannot be copied from A to B.
- The request and response **bodies** must flow across the gap without the
  gateway holding them.
- Connection **B** doesn't have to be created per request — it can be *reused*
  for the next request to the same backend.

The naive proxy ignores all three and works fine in a demo. Here is how each
one hurts you in production:

| Naive move | What actually happens | The scar |
|---|---|---|
| Read the whole body, then send it | A 1 GiB upload lives in your RAM — times every concurrent upload | OOM-kill under load that the backend would have handled fine |
| Copy every header "to be transparent" | Connection-level headers from hop A leak onto hop B | Request smuggling, broken keep-alive, security decisions delegated to strangers |
| New TCP connection per upstream request | Every request pays a handshake before byte one | The proxy *adds* more latency than the backend spends working |

The rest of this doc takes them one at a time.

---

## 2. Streaming: why proxy memory must not scale with body size

Suppose your gateway holds 10,000 concurrent connections and clients are
uploading 100 MiB files.

| Strategy | Memory formula | At 10,000 connections |
|---|---|---|
| **Buffer** each body fully | connections × body size | 10,000 × 100 MiB = **1 TiB** |
| **Stream** in chunks | connections × chunk size | 10,000 × 64 KiB = **625 MiB** |

That's the whole argument. Streaming makes memory
**O(connections × chunk)** instead of **O(connections × body)** — the body size
drops out of the formula entirely, which is why the SPEC's first Done-when
criterion is phrased as an *observable*: a 1 GiB upload must not grow RSS by
~1 GiB.

Streaming also gives you **backpressure for free**: if the backend reads
slowly, the gateway stops pulling from the client (the chunk in hand has
nowhere to go), TCP flow control pushes back on the client, and the *slow
reader sets the pace* instead of the gateway absorbing the difference as
buffered bytes. You met this exact mechanism on project 06's proxy path.

The scaffold already sets you up for this: `UpstreamClient` in
[main.rs](../src/main.rs) is a hyper client whose body type is axum's `Body`,
so an inbound request body can be *handed* upstream and an upstream response
body handed back — the design question V1 asks is how to do that end to end
**without ever collecting either body**.

> **Where streaming still breaks down (depth probe):** if you want to *retry* a
> failed request (see [doc 04](04-gateway-fundamentals.md)), you need the body
> again — but you already streamed it out. A streaming proxy can only freely
> retry requests whose bodies it hasn't consumed (or has cheaply buffered
> because they're small). Keep this tension in mind; it resurfaces in the
> retry horizontal.

---

## 3. Hop-by-hop vs end-to-end: the two classes of headers

HTTP headers split into two classes, and the split is the single most
important idea in V1:

- **End-to-end** headers describe *the message*: `Content-Type`, `Accept`,
  `Authorization`, `Cache-Control`, your app's custom headers. These belong to
  the request/response and must pass through.
- **Hop-by-hop** headers describe *one TCP connection* — one "hop": how long to
  keep it alive, how the body is framed *on that hop*, whether the hop is
  about to be upgraded to another protocol. These are meaningful only between
  two directly-connected peers and **must be stripped and re-derived on each
  hop**.

RFC 7230 §6.1 names the standard hop-by-hop set:

```
Connection, Keep-Alive, Proxy-Authenticate, Proxy-Authorization,
TE, Trailer, Transfer-Encoding, Upgrade
```

…plus a subtle rule: the `Connection` header can **name additional headers**
that become hop-by-hop for that connection. `Connection: close, X-Foo` means
`X-Foo` is hop-by-hop *here*, even though it isn't on the RFC list. A correct
proxy strips the listed set *and* anything the inbound `Connection` header names.

### Worked example

Inbound request on connection A:

| Header | Class | Forwarded to backend? |
|---|---|---|
| `Host: api.example.com` | end-to-end (but proxies set their own policy) | rewritten per your `X-Forwarded-Host` policy |
| `Content-Type: application/json` | end-to-end | ✅ yes, unchanged |
| `Authorization: Bearer …` | end-to-end | ✅ yes, unchanged |
| `Connection: keep-alive` | hop-by-hop | ❌ stripped — hop B manages its own keep-alive |
| `Transfer-Encoding: chunked` | hop-by-hop | ❌ stripped — hop B frames the body itself |
| `Upgrade: websocket` | hop-by-hop | ❌ stripped (unless you build the 101 stretch goal deliberately) |

### Why leaking these is a *security* bug, not a tidiness bug

Body framing (`Content-Length` vs `Transfer-Encoding`) is decided per hop. If
your gateway and the backend can be made to **disagree about where a request
body ends** — because a framing header you should have owned passed through
unexamined — then one HTTP connection can carry bytes that the gateway parsed
as *one* request but the backend parses as *two*. The second, "smuggled"
request never went through your routing, auth, or limits. This is the
CL.TE/TE.CL **request-smuggling** class of CVEs, and it exists *entirely*
because proxies forwarded hop-by-hop framing semantics they weren't honoring.

The trap named in [CONCEPTS.md](../CONCEPTS.md) is worth repeating:
**transparency is the bug**. A proxy that forwards everything has delegated
its security decisions to whoever crafts the request.

---

## 4. Provenance: `X-Forwarded-For`, `X-Forwarded-Proto`, `Via`

Once the gateway is in the middle, the backend no longer sees the client's IP —
every connection arrives from the gateway. The fix is a set of headers the
proxy *adds*:

- `X-Forwarded-For` — the chain of client IPs, one appended per proxy.
- `X-Forwarded-Proto` — was the *original* request `http` or `https`?
- `X-Forwarded-Host` — the `Host` the client originally asked for.
- `Via` — which proxies (and HTTP versions) the request passed through.

The catch: **the client can send these headers too, and lie.**

```
Attacker sends:            X-Forwarded-For: 127.0.0.1
                                    │
                 ┌──────────────────┴───────────────────┐
                 │ if the gateway passes it through:    │
                 │   backend sees "127.0.0.1" and       │
                 │   grants localhost-only admin access │
                 └──────────────────────────────────────┘
```

So a proxy never *trusts* an inbound `X-Forwarded-For`; it either **appends**
the real peer address it observed on the socket (`X-Forwarded-For: <inbound
value>, <real client IP>` — the last entry is the only one *you* vouch for) or,
at the very edge of your infrastructure, **replaces** the header entirely. The
SPEC doesn't pick for you — it requires that you pick one and *document the
policy*. Which you choose depends on one question: is anything upstream of
this gateway a proxy you trust?

---

## 5. Connection reuse: the handshake arithmetic

Opening a fresh connection to a backend costs round trips *before the request
can even be sent*. At a 20 ms RTT to the backend:

| Connection state | Round trips before byte 1 | Added latency |
|---|---|---|
| Pooled keep-alive connection | 0 | **~0 ms** |
| Fresh TCP | 1 (SYN → SYN-ACK) | **20 ms** |
| Fresh TCP + TLS 1.3 | 2 | **40 ms** |
| Fresh TCP + TLS 1.2 | 3 | **60 ms** |

If the backend does its actual work in 5 ms, an un-pooled proxy is spending
4–12× the useful work *per request* on handshakes. Pooling amortizes that to
~zero: after the first request, the connection sits in a per-host pool waiting
for the next one.

The scaffold hands you this: the `UpstreamClient` built in
[main.rs](../src/main.rs) (`hyper-util`'s `legacy::Client`) keeps a per-host
keep-alive pool automatically — *if you send requests through it*. The SPEC's
Done-when makes this observable: a burst of N requests to one backend must not
open N TCP connections. (This is also *why* hop-by-hop hygiene matters again:
a leaked `Connection: close` would sabotage exactly this reuse.)

---

## 6. When the upstream fails: the error taxonomy

A proxy's failure vocabulary is small and precise, and
[error.rs](../src/error.rs) already encodes it:

| Situation | Status | `AppError` variant |
|---|---|---|
| No route matched (V2) | 404 | `NoRoute` |
| Route matched, whole pool down/ejected (V3/V4) | 503 | `NoHealthyBackend` |
| Connect refused / reset / DNS failure | 502 | `BadGateway` |
| Upstream didn't answer within the deadline | 504 | `GatewayTimeout` |

Two rules the Done-when enforces:

1. **Never hang.** Every proxied request carries a deadline
   (`AppState.request_timeout`, default 10 s in [main.rs](../src/main.rs));
   when it expires the client gets a clean 504, not silence.
2. **Never panic.** A dead backend is a Tuesday, not an exception. Transport
   errors map to 502 via `?` like any other handled error.

The 502-vs-504 distinction matters operationally: 502 means "I couldn't
*reach* it" (network, dead process), 504 means "I reached it and it's *too
slow*" (overload, lock-up). They page different investigations — and V4 will
treat both as circuit-breaker food.

---

## 7. The design space V1 leaves to you

The scaffold gives you the client, the types, and the error vocabulary. What
`forward()` must decide — and what makes it interesting:

- **How to strip hop-by-hop headers completely** — the fixed RFC list is the
  easy half; honoring headers *named by* `Connection`, in both directions, is
  the part people miss.
- **Your `X-Forwarded-For` trust policy** — append vs replace, and writing it
  down.
- **How to keep both bodies streaming** while still enforcing the request
  deadline and the body cap (`AppState.max_body_bytes`) — bounding a body you
  refuse to buffer is a genuinely good puzzle.
- **How transport errors and timeouts map** onto `BadGateway` / `GatewayTimeout`
  without letting any path hang or panic.

That's the vertical. When you're ready to build, `/quest` will pin these down
as failing acceptance tests first; if you get stuck mid-build, `/hint` gives
graduated nudges without spoiling the path.

---

## Mental-model summary

| Concept | The model |
|---|---|
| A proxy | Two connections + a rewriter, never one pipe |
| Streaming | Memory = O(connections × chunk); body size drops out; slow readers set the pace |
| Hop-by-hop headers | Describe *one hop*; strip the RFC 7230 §6.1 list **plus** whatever `Connection` names |
| Smuggling | What happens when two parsers disagree about where a body ends |
| `X-Forwarded-For` | A chain where you only vouch for the entry *you* appended |
| Pooling | Handshakes amortized to ~0; a leaked `Connection: close` un-does it |
| 502 / 504 / 503 / 404 | Can't reach it / too slow / no healthy pool / no route |

## Where you'll build this

**Module:** [src/proxy.rs](../src/proxy.rs) — the `todo!()` in `forward()` is
the whole byte path: rewrite → strip hop-by-hop → stream upstream via the
pooled client → stream the response back.

**This doc unlocks V1's Done-when criteria** ([SPEC.md](../SPEC.md) §V1):
bounded-memory streaming, hop-by-hop stripping, provenance headers with a
documented trust policy, observable connection reuse, end-to-end preservation
of method/path/status/headers, and clean 502/504 on upstream failure.
