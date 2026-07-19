<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 10 — API Gateway / L7 Reverse Proxy

> "Just forward the request" is a lie you tell yourself until the first 2 GiB
> upload buffers into RAM, a client smuggles a `Transfer-Encoding` header past you
> to the backend, one slow upstream drags every request to its timeout, and a dead
> node turns a single failure into a full outage. An API gateway sits in front of a
> fleet and has to do the hard parts *for* everyone: route by path/host, spread load
> across a pool, notice a backend is dying and stop sending it traffic, terminate
> TLS, and stay a thin, streaming, memory-bounded hop no matter how big the bodies
> get. This project builds that gateway from the byte path up — the forwarding core,
> a routing engine, a load balancer, and circuit breaking — the parts you'd normally
> reach for `nginx`/`Envoy`/`tower::balance` to do.

## What it does (the easy part)
- Listens on one port and **proxies** every request to an upstream chosen by its
  route — the client talks only to the gateway and never sees the topology.
- `GET /admin/routes` → the loaded route table (name + path prefix + backends).
- `GET /healthz` → the gateway's own liveness (`200 ok`).
- `GET /metrics` → Prometheus scrape.
- Routes come from a JSON config (`CONFIG_PATH`) or a built-in catch-all over
  `UPSTREAM_BACKENDS`, so a bare `cargo run` proxies to a pool with zero files.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The reverse-proxy forwarding core — *build the byte path*
The proxy's job looks trivial and isn't. You must **stream** bodies (never buffer a
big upload in RAM), keep hop-by-hop headers from leaking between connections, add
the `X-Forwarded-*`/`Via` provenance headers, and **reuse** upstream keep-alive
connections or you pay a fresh TCP (and TLS) handshake on every single request.
Build the forwarding path in `src/proxy.rs` instead of reaching for a
`hyper-reverse-proxy` crate.

**Done when ALL true:**
- [ ] A request/response body is **streamed** through the proxy — memory stays bounded regardless of body size (a 1 GiB upload does not grow RSS by ~1 GiB).
- [ ] **Hop-by-hop headers** (`Connection`, `Keep-Alive`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, `Proxy-*`) are stripped between hops; only end-to-end headers pass through — verifiable by asserting the backend never receives them.
- [ ] The proxy sets provenance headers: it **appends** to `X-Forwarded-For`, sets `X-Forwarded-Proto`/`Host`, and adds a `Via` — a client-supplied `X-Forwarded-For` is not blindly trusted (appended-to or replaced, per a documented policy).
- [ ] **Upstream connections are pooled/reused**: a burst of N requests to one backend does **not** open N fresh TCP connections (keep-alive) — observable by connection count.
- [ ] Method, path, query, response status and (non-hop-by-hop) headers are **preserved end to end** — what the backend returns is what the client sees.
- [ ] An unreachable or slow upstream yields a clean **502/504**, never a panic and never a hung request.

**Proof:** an integration test that proxies to a local test server and asserts the
backend saw *no* hop-by-hop headers and *did* see an appended `X-Forwarded-For`; a
streaming test that a large body round-trips without buffering; `bench/` records the
p50/p99 latency the proxy *adds* vs hitting the backend directly → `docs/10-benchmarks.md`.

*Concept to internalize:* hop-by-hop vs end-to-end headers (RFC 7230 §6.1), why
streaming bodies keeps memory bounded, and how connection pooling amortizes the
handshake. **Stretch:** proxy a `101 Switching Protocols` (WebSocket) upgrade end to end.

### V2. The request routing engine — *match, don't scan*
A gateway maps many inbound `(host, path, method)` tuples to upstreams. The naive
`for r in routes { if path.starts_with(r.prefix) }` is O(routes) per request and gets
ambiguous fast: does `/api/v2/users` belong to `/api` or `/api/v2`? Build the matcher
in `src/router.rs` so it resolves **longest-prefix** deterministically and in time
that doesn't grow linearly with the route count.

**Done when ALL true:**
- [ ] A request resolves to a route by **host + path prefix + method**; the same request always resolves to the same route for a given table.
- [ ] **Longest-prefix wins:** with both `/api` and `/api/v2` registered, `/api/v2/x` resolves to `/api/v2` — deterministically, not by insertion order.
- [ ] **Host and method constraints** are honoured: a route scoped to `POST` or `host: api.example.com` does not match other methods/hosts.
- [ ] An unmatched request returns a clean **404 (no route)** — distinct from *matched-route-but-backend-down* (503/502).
- [ ] Match cost is **sub-linear** in the number of routes (a prefix tree / sorted structure, not a per-request scan of every route) — demonstrable as the table grows.
- [ ] The route table can be **rebuilt/swapped** (config reload) without dropping in-flight requests.

**Proof:** unit tests for longest-prefix precedence, host/method scoping, and the 404
case; a test or bench showing match latency stays roughly flat as routes grow
10 → 10k; `docs/10-design.md` names the matching structure you chose and why.

*Concept to internalize:* prefix/radix matching, longest-match precedence, and why
routing sits on the hot path of *every* request. **Stretch:** path parameters
(`/users/{id}`) or regex routes with an explicit precedence rule.

### V3. Load balancing across a backend pool — *`round-robin` is the floor, not the goal*
A route points at a **pool** of N backends; something must pick one per request.
Round-robin ignores that backend #3 is slow and piling up; naive random gives unlucky
hot spots. Build the balancer in `src/balancer.rs` — start round-robin, then
**least-connections**, then **P2C (power of two choices)** with an EWMA latency
signal — and measure *why* P2C beats round-robin under uneven load.

**Done when ALL true:**
- [ ] Across many requests a healthy pool gets a **balanced** share (round-robin/P2C spread even within tolerance) — no backend starved or hammered.
- [ ] The balancer tracks **in-flight per backend**, and least-connections routes the next request to the least-loaded backend.
- [ ] The policy is **swappable** (round-robin / least-conn / P2C) behind one interface, and a test shows they make **different** choices on the same skewed trace.
- [ ] Backends that are **unhealthy / open-circuit** (V4) are **excluded** from selection — the balancer never hands back an ejected backend.
- [ ] Selection is **cheap on the hot path** (atomics / per-shard state, not one global mutex serializing every pick) — a documented decision.
- [ ] Under one slow backend, **P2C measurably shifts load away** from it vs round-robin (lower p99) — shown by a bench.

**Proof:** a distribution test (even spread across a healthy pool); a test that an
unhealthy backend is never picked; a bench comparing p99 under one slow backend for
round-robin vs P2C in `docs/10-benchmarks.md`.

*Concept to internalize:* LB algorithms and their failure modes, why "the power of two
random choices" beats both pure random and round-robin, and the in-flight/EWMA signal.
**Stretch:** weighted backends, or subsetting so a huge pool doesn't fan health checks
everywhere.

### V4. Health checking & circuit breaking — *fail fast, don't cascade*
A dead backend you keep sending traffic to turns one failure into a latency cascade:
every request waits the full timeout, connections pile up, and the gateway falls over
*with* it. Build **active health checks** (periodic probes eject a dead backend) and
**passive circuit breaking** (a run of failures *opens* the circuit so calls fail fast,
then *half-open* probes let it recover) in `src/health.rs`. This is the line between
"one backend is down" and "the whole gateway is down".

**Done when ALL true:**
- [ ] **Active probes** check each backend on an interval; one that fails is **ejected** from the pool and a recovered one is **re-added** — automatically, no restart.
- [ ] **Passive outlier detection:** a backend returning a run of errors/timeouts on live traffic is pulled from rotation even between active probes.
- [ ] A per-backend **circuit breaker** implements `Closed → Open → HalfOpen`: past a failure threshold it **opens** and calls to it **fail fast** — no upstream call, no waiting the timeout.
- [ ] After a cooldown the circuit goes **half-open** and admits a **limited** number of trial requests; success **closes** it, failure re-opens it — no flapping storm.
- [ ] With one backend **hard-down**, client-visible latency **stays low** (fail-fast + reroute to a healthy backend) instead of every request paying the full timeout.
- [ ] Circuit transitions are **observable** (log/metric) so you can watch a backend trip and recover.

**Proof:** an integration test that kills a backend and asserts (a) it's ejected
within the probe interval, (b) requests still succeed via a healthy backend, and
(c) once the circuit is open, calls to the dead backend return **well under** the
upstream timeout; `docs/10-design.md` records the probe interval, failure threshold,
open cooldown, and half-open trial count.

*Concept to internalize:* the circuit-breaker state machine, active vs passive health,
and why fail-fast + load-shedding is what prevents a cascading failure. **Stretch:**
success-rate outlier ejection (Envoy-style) with gradual re-introduction.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] Listens on **HTTP/1.1 and HTTP/2** (h2 to clients); upstream requests reuse keep-alive (V1). Chunked/streaming bodies work in both directions.
- [ ] **Hop-by-hop hygiene + `X-Forwarded-*`/`Via`** handled correctly (V1), and a client **cannot spoof** `X-Forwarded-*` or internal auth headers through the proxy (normalized at the edge).
- [ ] **WebSocket / `Upgrade`** is proxied end to end (`101` passthrough), or `docs/10-design.md` records it as explicitly out of scope.
- [ ] **Graceful shutdown:** on SIGTERM stop accepting new connections and **drain in-flight proxied requests** within a deadline before exit — no truncated responses.

### Caching / resilience
- [ ] **Upstream connection pooling** (keep-alive reuse) so the hot path doesn't pay a handshake per request (V1).
- [ ] **Request coalescing / single-flight** for identical in-flight idempotent GETs (optional micro-cache) — *or* a documented decision to defer caching to the origin. (ties back to project 01's stampede protection)
- [ ] **Retries with a budget + timeouts:** only **idempotent** requests retry, retries are **capped** (a retry budget, not blind ×3 amplification), and every proxied request carries a **deadline** (connect + overall). Recorded in `docs/10-design.md`.
- [ ] **Backpressure / load-shedding:** a global concurrency limit sheds load (fast `503`) instead of unbounded queueing when every backend is saturated.

### Security
- [ ] **mTLS** (`src/tls.rs`): the gateway terminates TLS from clients and can be configured to **present a client cert to upstreams** and verify the upstream cert — a mutually-authenticated data path; optionally **require + verify client certs** from callers (mTLS at the edge). Trust roots/paths come from config, never hard-coded.
- [ ] **Edge auth:** API-key/JWT (or similar) validated at the gateway *before* forwarding; an unauthenticated request is rejected without touching an upstream. Keys/secrets never logged.
- [ ] **Request limits:** max header size, max body size (`MAX_BODY_BYTES`), and per-request timeouts bound slowloris / oversized-body abuse — each returns the right 4xx (`413`/`431`/`408`).
- [ ] **Header sanitization:** hop-by-hop and sensitive inbound headers are stripped/normalized so a client can't impersonate the proxy to the backend.

### Observability
- [ ] `tracing` span per proxied request (via `common-telemetry`) with a request id, the **matched route**, the **chosen backend**, upstream status, and upstream latency as structured fields.
- [ ] Metrics at `/metrics`: **requests by route+status, upstream latency histogram, in-flight per backend, LB picks per backend, retries, and circuit-breaker state/trips.**
- [ ] An **access log** line per request (method, path, route, backend, status, total + upstream latency) — enough to debug a bad backend from logs alone.

---

## Cross-cutting scale skills (every project carries these)
- **Backpressure & bounds:** bounded body size, a global concurrency limit / load-shed,
  and a bounded retry budget — no unbounded queue anywhere on the request path.
- **Graceful shutdown:** drain in-flight proxied requests within a deadline on SIGTERM.
- **Benchmarks with numbers:** `bench/` + `docs/10-benchmarks.md` — the latency the
  proxy adds (p50/p99) vs direct, throughput, LB fairness, and fail-fast latency with
  a dead backend (the circuit-breaker payoff).

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains (a) added proxy latency (p50/p99) vs direct-to-backend and
   throughput, (b) LB distribution across a healthy pool + p99 under one slow backend
   for round-robin vs P2C, and (c) client-visible latency with one backend hard-down
   (the fail-fast payoff) — numbers in `docs/10-benchmarks.md`.
3. `docs/10-design.md` records the decisions the SPEC grades: **routing match
   structure, LB policy + why, circuit-breaker timings (threshold/cooldown/half-open),
   retry + timeout budget, and the mTLS trust model.**
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p api-gateway` are green;
   no `todo!()` remains on a checked path.

## Suggested order of attack
1. Single hardcoded upstream: forward the request, stream the response back, get
   hop-by-hop headers + `X-Forwarded-For` right (V1).
2. Add the routing table so `(host, path, method)` selects an upstream — longest-prefix
   match (V2).
3. Give a route a **pool** and load-balance across its backends (V3).
4. Add active health checks + per-backend circuit breaking so a dead backend is ejected
   and fails fast (V4).
5. Add resilience (retry budget + timeouts + concurrency-limit/load-shed), then mTLS +
   edge auth + request limits.
6. Benchmark (proxy overhead, LB fairness, fail-fast), document, tune.

## Run the demo
```bash
cp .env.example .env
cargo run -p api-gateway            # gateway on :8080, proxying per UPSTREAM_BACKENDS

# Or the whole demo: the gateway + a pool of 3 echo backends
docker compose up --build
curl localhost:8080/                # whoami — the backend name changes across requests (LB)
docker compose stop whoami-b        # kill one backend...
curl localhost:8080/                # ...still 200 from a healthy backend (fail-fast + reroute)
curl localhost:8080/admin/routes    # the loaded route table
```
