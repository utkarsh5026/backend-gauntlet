# Health Checks & Circuit Breaking — Fail Fast, Don't Cascade

> **What this teaches:** why one dead backend can take down an entire gateway —
> not by sending errors, but by sending *slowness* — and the two-layer defense
> (active probes + a per-backend circuit breaker) that stops the cascade. No prior
> knowledge assumed. This prepares you for **V4** in [SPEC.md](../SPEC.md): the
> `CircuitBreaker` state machine and `HealthChecker::run()` in
> [health.rs](../src/health.rs), which the balancer ([balancer.rs](../src/balancer.rs))
> and proxy path already call through `allow` / `record_success` / `record_failure`.

---

## The one sentence to hold onto

**Slow is worse than down: a dead backend that *times out* occupies your
resources for the full timeout on every doomed request, and the circuit
breaker's whole job is to convert that expensive discovery into an instant,
free "no".**

---

## 2. The cascade: how one dead backend kills a healthy gateway

Walk the arithmetic. Your gateway forwards to a pool; one backend dies in the
worst way — it doesn't refuse connections (that would fail in milliseconds),
it **accepts and never responds**. Requests to it wait the full upstream
timeout: 5 s ([main.rs](../src/main.rs)'s `REQUEST_TIMEOUT_MS`-style deadline).

Say this backend's share of traffic is 200 req/s. Little's law (occupancy =
arrival rate × time in system):

```
200 req/s  ×  5 s timeout  =  1,000 requests permanently in flight
```

Those 1,000 doomed requests are not just numbers — **each one holds real
resources for its entire 5 seconds**:

| Held per doomed request | Consequence at 1,000 concurrent |
|---|---|
| A client connection + a gateway task | connection/task limits approached |
| An upstream connection *attempt* | pool slots and FDs consumed |
| A slot under any concurrency cap | **healthy** traffic queues behind doomed traffic |
| The *client's* patience (they may retry!) | arrival rate goes *up* as service degrades |

And the client-visible latency: with 3 backends round-robining, ~⅓ of all
requests now take 5,000 ms instead of 10 ms. Queues fill with work that is
already dead; healthy backends starve behind the jam; the gateway exhausts
tasks/connections and falls over *with* the backend. One node's failure has
become everyone's outage — that's a **cascading failure**, and the mechanism
was never errors. It was *occupancy*.

Compare the alternative: if the gateway somehow *knew* the backend was dead
and answered those requests instantly (fail fast) or rerouted them, the same
outage costs microseconds per affected request and the platform barely
notices. Detection + fail-fast is the entire game. Note that a timeout alone
doesn't save you — the trap in [CONCEPTS.md](../CONCEPTS.md) — because a
timeout bounds *one* request's wait; the cascade is about the *aggregate*:
with only timeouts you still hold rate × timeout of doomed work at all times.

---

## 2. Detection layer 1: active health checks

The simplest detector: **ask**. A background task probes every backend on an
interval and flips its eligibility:

```
every interval (e.g. 2 s):
    for each backend in router.backends():
        send a cheap probe (e.g. GET /healthz, short timeout)
        healthy  → backend eligible   (re-admit if ejected)
        unhealthy → backend ejected   (balancer stops picking it)
```

The scaffold's `HealthChecker` ([health.rs](../src/health.rs)) is exactly this
shape — router + client + interval, with `run()` as the `todo!()`, and a
`TODO(V4)` in [main.rs](../src/main.rs) marking where it gets spawned. The
router already exposes the whole fleet via `Router::backends()`.

Active probes give you a hard bound — a dead backend is caught within one
interval, **even if it dies while idle** at 3 a.m. with zero traffic. But
they have blind spots:

| Active probes miss… | Because… |
|---|---|
| Failures between probes | a backend can die 1 ms after passing a probe and eat traffic for a whole interval |
| Request-path-only failures | `/healthz` returns 200 while `/api/orders` 500s (probe checks liveness, not the real work) |
| Partial degradation | the probe (tiny, fast) succeeds while real requests (big, slow) time out |

So you need a second detector that watches what probes can't: the real traffic.

## 3. Detection layer 2: passive outlier detection

**The live traffic is itself a continuous health check** — every proxied
request already tells you whether that backend is working. Passive detection
just listens: count consecutive (or rate-of) errors and timeouts per backend,
and when a backend crosses the threshold, pull it from rotation *immediately*,
between probes. The hooks are already in the scaffold's interfaces:
`record_success` / `record_failure` get called from the proxy path's outcome.

The two layers are complementary, and the SPEC requires both:

| | Active probes | Passive observation |
|---|---|---|
| Catches idle-period death | ✅ within one interval | ❌ no traffic, no signal |
| Catches request-path-only failure | ❌ probe path lies | ✅ sees the real requests fail |
| Detection latency under traffic | up to a full interval | a handful of requests (~ms) |
| Costs | probe traffic to every backend | free — piggybacks on real work |

## 4. The policy: the circuit-breaker state machine

Detection says "this backend is bad". The **circuit breaker** turns that into
the fail-fast behavior, per backend. The name is the household electrical
breaker: a fault *trips* it, current stops flowing *instantly*, and you reset
it deliberately — it doesn't keep re-electrifying the fault to check.

Three states ([health.rs](../src/health.rs)'s `CircuitState`):

```
                    failures ≥ threshold
        ┌──────────┐ ─────────────────────▶ ┌──────────┐
        │  CLOSED  │                        │   OPEN   │ ─── allow() = false:
        │ (normal) │                        │(fail fast)│    fail instantly,
        └──────────┘ ◀──────┐               └──────────┘    no upstream call
              ▲             │                     │
              │       trial successes      cooldown elapsed
              │             │                     ▼
              │        ┌────┴───────┐  any trial fails
              └─?──────│  HALF-OPEN │ ────────────────▶ back to OPEN
                       │ (probing)  │
                       └────────────┘
```

- **Closed** — normal operation. Failures are counted; enough of them
  (the **threshold**) trips the circuit open.
- **Open** — the fail-fast state. `allow()` returns `false`: requests to this
  backend are refused *without any upstream attempt* — no connection, no
  timeout wait, microseconds instead of 5 seconds. The balancer skips it
  (`is_available()`); if somehow reached, the caller gets an instant error
  and reroutes.
- **Half-open** — after a **cooldown**, the breaker must find out whether the
  backend recovered. It admits a **limited number of trial requests** (the
  **half-open permit count**). Enough successes → **close** (recovered);
  any failure → **re-open** and restart the cooldown.

Each parameter exists to prevent a specific pathology — this is what
`docs/10-design.md` asks you to record values *and reasons* for:

| Parameter | Too low | Too high |
|---|---|---|
| Failure threshold | one network blip ejects a healthy backend (flapping) | a dying backend eats traffic for too long before tripping |
| Open cooldown | you hammer a backend that's mid-restart | a recovered backend sits idle, pool runs degraded |
| Half-open permits | recovery detection is slow/noisy (1 sample) | see below — the recovery stampede |

**Why half-open must be *limited*** deserves its own paragraph, because the
naive version re-creates the outage: cooldown expires, the circuit naively
lets *all* traffic through "to see", and a backend that just restarted — cold
caches, connection pools empty, JIT cold — receives its full production share
in one instant. It buckles, the circuit re-opens, cooldown expires, repeat.
That's a **flapping storm**: the breaker itself DDoSes the patient it's
monitoring. A limited trial count makes recovery a *probe*, not a stampede —
the same "gradual re-introduction" idea Envoy's outlier ejection formalizes.

### Two design questions the SPEC leaves to you

- **Count-based vs rate-based thresholds** (the depth probe in
  [CONCEPTS.md](../CONCEPTS.md)): "3 consecutive failures" means something
  completely different at 2 req/min (90 s of evidence — fine) vs 2,000 req/s
  (1.5 ms of evidence — one packet burst trips it). Consecutive-count is
  simple and fine to start; know what it costs and what a failure-*rate*
  window buys.
- **The whole pool trips open — then what?** Every backend's circuit is open;
  `pick()` returns `None`. Pretending (sending traffic anyway) hides the truth;
  the honest answer is load-shedding — a fast `NoHealthyBackend` 503
  ([error.rs](../src/error.rs)) — plus loud observability. Some real systems
  choose "fail open" here (if *everything* looks dead, maybe the detector is
  what's broken). Decide and document.

### The hot-path constraint

`allow()` is consulted on **every request** (the balancer calls
`is_available()` on candidates before picking). That's why the scaffold stores
the state as an `AtomicU8` — the common case (closed, healthy) must be a
single lock-free load, not a mutex acquisition. The state machine's
*transitions* are rare; its *reads* are constant. Keep the read path free; the
scaffold's comment about adding more atomics (failure run, `opened_at`,
half-open permits) is pointing at the same discipline.

And make transitions **observable** — a log line and a metric per state change
(the SPEC's last Done-when box). A circuit that trips silently is a backend
that vanished from your pool with no explanation; watching
`circuit_state{backend="…"}` flip Open → HalfOpen → Closed during the
docker-compose demo (`docker compose stop whoami-b`) is V4's payoff made
visible.

---

## Mental-model summary

| Concept | The model |
|---|---|
| The cascade | Occupancy = rate × timeout; slow failures hold resources, errors don't |
| Slow vs down | Down fails in ms and is cheap; slow fails in `timeout` and is ruinous |
| Active probes | Ask on an interval — bounds detection even at zero traffic; can be lied to |
| Passive detection | Real traffic is a free, continuous health check of the real path |
| Circuit breaker | Detection → policy: Closed counts, Open refuses instantly, Half-open probes |
| Half-open limit | Recovery is a probe, not a stampede — unthrottled retry re-kills the patient |
| Threshold semantics | A count means nothing without the traffic rate behind it |
| All-open pool | Shed load honestly (fast 503) — and page, don't pretend |
| Hot path | `allow()` is one atomic load in the common case |

## Where you'll build this

**Module:** [src/health.rs](../src/health.rs) — four `todo!()`s make the
state machine (`allow`, `record_success`, `record_failure`, `state`) plus the
probe loop (`HealthChecker::run`), then the spawn `TODO(V4)` in
[main.rs](../src/main.rs). The balancer already refuses unavailable backends
through `Backend::is_available()`; V1's outcome handling is where
success/failure records feed in.

**This doc unlocks V4's Done-when criteria** ([SPEC.md](../SPEC.md) §V4):
interval probes that eject and re-admit automatically, passive outlier
detection between probes, a fail-fast `Closed → Open → HalfOpen` breaker with
limited trials and no flapping, low client latency with one backend hard-down,
and observable transitions — with the timings recorded in `docs/10-design.md`.
