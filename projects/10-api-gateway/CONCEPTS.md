# Concept Bank — Project 10: API Gateway / L7 Reverse Proxy

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The byte path: headers, streaming, connection reuse *(V1 · `src/proxy.rs`)*

**The problem.** "Just forward the request" hides three ways to hurt yourself. Buffer bodies and a single 2 GiB upload lives in your RAM (times concurrency). Forward headers blindly and hop-by-hop headers leak between connections — the raw material of request-smuggling attacks (`Transfer-Encoding` disagreements between you and the backend are a classic CVE category). Open a fresh upstream TCP connection per request and you pay a handshake (plus TLS) on every single hop — often more time than the backend spends working.

**The idea.** Stream bodies chunk-by-chunk in both directions (memory bounded by a chunk, backpressure via flow control — project 06's lesson on the proxy path). Know the two header classes: **hop-by-hop** headers (`Connection`, `Transfer-Encoding`, `Upgrade`, …) describe *one TCP hop* and must be stripped and re-derived per hop; end-to-end headers pass through. Add provenance (`X-Forwarded-For` appended — never trusted from the client, `X-Forwarded-Proto`, `Via`). And pool upstream connections so keep-alive amortizes handshakes to ~zero.

**In the wild:** nginx, Envoy, HAProxy, Cloudflare's edge — this byte path is their core; the smuggling class of attacks (CL.TE/TE.CL) is entirely about proxies mishandling hop-by-hop semantics.

**You own it when you can explain:**
- [ ] Hop-by-hop vs end-to-end headers: the definition, the RFC 7230 §6.1 list, and one concrete exploit if `Transfer-Encoding` passes through unexamined.
- [ ] Why streaming makes proxy memory O(connections × chunk) instead of O(connections × body).
- [ ] The `X-Forwarded-For` trust problem: why append (or replace at the edge) rather than pass through — a client can arrive claiming to be 127.0.0.1.
- [ ] What connection pooling saves, with the handshake arithmetic (TCP RTT + TLS round trips vs ~0 for a pooled connection).
- [ ] The error taxonomy: unreachable upstream → 502, timeout → 504 — and why the client must never just hang.

**Depth probes:**
- Proxying a WebSocket upgrade end-to-end: which header rules bend, and what does the proxy become after `101` (a dumb byte pipe)?
- Where can a streaming proxy still be forced to buffer (retry-ability of a consumed request body)?

**Trap:** copying all headers "to be transparent". Transparency is the bug — a proxy that forwards `Connection: keep-alive` semantics it isn't honoring, or a spoofed `X-Forwarded-For`, has delegated its security decisions to strangers.

---

## 🧠 Card 2 — The routing engine *(V2 · `src/router.rs`)*

**The problem.** Route `/api/v2/users` when both `/api` and `/api/v2` are registered: which wins, and *by rule or by accident*? A linear scan over routes answers "whichever was inserted first matched first" — order-dependent behavior that changes when someone reorders a config file — and its cost grows with every route added, on the hot path of literally every request.

**The idea.** Longest-prefix match as an explicit, deterministic rule, computed on a structure built for it (a prefix/radix tree or sorted keys + binary search) so match cost tracks *path length*, not route count. Host and method are additional constraints; "no route" (404) is deliberately distinct from "route found, backend down" (502/503) because they page different teams. Config reload swaps the whole table atomically under an `Arc` — in-flight requests finish on the old table.

**In the wild:** nginx `location` precedence rules, Envoy route tables, Kubernetes Ingress path types (`Prefix` vs `Exact`), axum/actix routers — all wrestle this exact precedence problem.

**You own it when you can explain:**
- [ ] Longest-prefix-wins as the deterministic answer to nested prefixes, and why insertion-order matching is a production incident waiting for a config reorder.
- [ ] Why sub-linear matching matters here specifically — multiply match cost by every request the platform serves.
- [ ] How a radix tree walks a path in O(path length) regardless of 10 vs 10,000 routes.
- [ ] The 404-vs-502 distinction as operational signal (misrouted client vs broken backend).
- [ ] Atomic table swap on reload: why in-flight requests keep the old table and new ones see the new — no lock on the hot path.

**Depth probes:**
- Add path parameters (`/users/{id}`) and regex routes: define a precedence rule that stays deterministic (exact > param > wildcard?).
- Host-based routing with wildcards (`*.example.com`) — how does the structure change?

**Trap:** benchmarking routing with 5 routes. The naive scan wins at 5 routes; the design question is the *curve*, and it only bends at scale.

---

## 🧠 Card 3 — Load balancing: from round-robin to P2C *(V3 · `src/balancer.rs`)*

**The problem.** A pool of N backends, one choice per request. Round-robin treats them as identical — but backend #3 is running a GC, and round-robin keeps feeding it while its queue grows and its latency poisons every Nth request. Pure random has unlucky streaks. Full least-connections needs a global scan (and can herd: everyone piles onto the "least loaded" node simultaneously). The pick must also be nearly free — it's on every request.

**The idea.** Track in-flight per backend (a cheap live load signal). Then **power of two choices**: sample two random backends, pick the less loaded. That one trick gets most of least-connections' benefit with O(1) work and — the subtle part — the randomness *breaks the herding* that deterministic least-loaded creates. Blend in EWMA latency and the signal notices slow-but-accepting backends too. Unhealthy/open-circuit backends are excluded before the pick.

**In the wild:** the "Power of Two Random Choices" result is one of the most-cited ideas in systems; Envoy `LEAST_REQUEST` *is* P2C; Finagle and Linkerd use P2C+EWMA; NGINX Plus offers it.

**You own it when you can explain:**
- [ ] Round-robin's blind spot with the slow-backend scenario, traced to its p99 effect.
- [ ] Why deterministic least-loaded can *herd* (a fresh, empty backend gets the whole next wave) and how P2C's sampling defuses it.
- [ ] The P2C result in plain words: two random samples ≈ exponential improvement over one, most of the value of full knowledge at none of the cost.
- [ ] What EWMA latency adds over in-flight count alone (detects slow-but-not-saturated).
- [ ] Why the pick must avoid a global mutex — atomics/sharded counters on the hot path.

**Depth probes:**
- With 2 gateway instances each running P2C on local in-flight counts, does the property survive? What if there were 50 instances (stale/local signals)?
- Weighted backends (a 2× box): where does weight enter round-robin vs P2C?

**Trap:** evaluating balancers by *distribution evenness* on healthy pools. They all pass that. The differentiator is behavior under *asymmetric degradation* — which is why the SPEC benches p99 with one slow backend.

---

## 🧠 Card 4 — Health checks & circuit breaking: fail fast, don't cascade *(V4 · `src/health.rs`)*

**The problem.** A backend dies. Every request routed to it now waits the full timeout — say 5 s — holding a connection, a task, and a client the whole time. Queues fill with doomed work; healthy backends starve behind it; latency climbs platform-wide; the gateway itself runs out of resources. One dead backend has become a full outage — not by sending errors, but by sending *slowness*. Slow is worse than down.

**The idea.** Two detection layers plus a policy. **Active probes** (periodic health pings) eject dead backends within an interval; **passive outlier detection** watches live traffic and pulls a backend that starts erroring *between* probes. The **circuit breaker** turns detection into fail-fast: per-backend `Closed → Open → HalfOpen` — past a failure threshold the circuit opens and calls fail *immediately* (no upstream attempt, no timeout wait); after a cooldown, half-open admits a few trial requests; success closes, failure re-opens. Recovery is probing, not flapping.

**In the wild:** the pattern name is from Nygard's *Release It!*; Envoy outlier ejection, Hystrix/Resilience4j, Finagle failure accrual, AWS ALB health checks — universal in service meshes.

**You own it when you can explain:**
- [ ] The cascade mechanics: why a *slow* dependency propagates failure upward in a way a *fast-failing* one doesn't (resource occupancy × timeout).
- [ ] Active vs passive detection — what each catches that the other misses (probes miss request-path-only failures; passive misses idle-period death).
- [ ] The three circuit states with their transition rules, and the purpose of each parameter (threshold, cooldown, half-open trial count).
- [ ] Why half-open must admit a *limited* number of trials — the un-throttled recovery stampede that re-kills a barely-recovered backend.
- [ ] Fail-fast + reroute as the user-visible payoff: client p99 stays low with one backend hard-down.

**Depth probes:**
- Circuit thresholds under low traffic: 3 failures in a row means what at 2 req/min vs 2k req/s? (Rate-based vs count-based thresholds.)
- The whole pool trips open. What should the gateway do — and what does "no healthy backends" load-shedding look like vs pretending?

**Trap:** timeouts as the only defense. Timeouts bound *one* request's wait; the cascade is about *aggregate* occupancy. Without the breaker you still hold N × timeout of doomed work at all times.

---

## ⚡ Rapid-fire round

- [ ] The retry rules as a trilogy: idempotent-only (a retried POST can double-charge), budgeted (a fixed retry *ratio*, not blind ×3 — or you amplify an outage into a self-DDoS), and deadline-bounded (connect + total).
- [ ] Load shedding: why a fast 503 at a concurrency cap beats queueing (queues add latency to *everyone* and hide the overload).
- [ ] mTLS: what each side proves, where trust roots live (config, never hard-coded), and what "the gateway presents a client cert to upstreams" defends against.
- [ ] Slowloris: what it exhausts and which limits (header timeout, body size, request timeout) close it.
- [ ] Header sanitization at the edge: strip inbound `X-Forwarded-*` / internal-auth headers so a client can't impersonate the proxy.
- [ ] The access-log line as the debugging contract: method, route, backend, status, total + upstream latency — enough to find a bad backend from logs alone.

## 🔗 Connects to

- Single-flight/coalescing for idempotent GETs ties back to project 01's stampede and forward to project 16's edge.
- The circuit breaker's fail-open/fail-closed philosophy echoes project 02's Redis-down policy.
- This gateway is the front door the rest of the gauntlet's services would sit behind — the horizontal concerns (limits, auth, observability) are its whole job.
