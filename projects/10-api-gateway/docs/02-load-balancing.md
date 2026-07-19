# Load Balancing — From Round-Robin to the Power of Two Choices

> **What this teaches:** how a gateway picks *one* backend out of a pool for each
> request, why the obvious strategies fail in non-obvious ways, and why sampling
> two random backends is one of the best ideas in distributed systems. No prior
> knowledge assumed. This prepares you for **V3** in [SPEC.md](../SPEC.md): the
> `pick()` you'll build in [balancer.rs](../src/balancer.rs), on top of the
> per-backend signals (`in_flight`, `ewma_micros`, `circuit`) the scaffold
> already tracks.

---

## The one sentence to hold onto

**All balancers look equal on a healthy pool; the real test is one degraded
backend — and *two random samples compared by live load* gets you most of the
benefit of perfect knowledge at almost none of the cost, while its randomness
prevents the herding that perfect knowledge causes.**

---

## 1. The setup — and the scenario that separates the policies

A route in this gateway points at a **pool**: in
[config.rs](../src/config.rs), an `UpstreamConfig` holds `backends` plus an
`lb` policy; [router.rs](../src/router.rs) compiles that into a `Balancer`
over `Arc<Backend>`s. Every proxied request asks `Balancer::pick()` for one
backend. Millions of times a day. The pick must be *good* and it must be
*nearly free*.

Here's the scenario that exposes every naive policy — hold onto it for the
whole doc:

> Pool of 3 backends. **B3 hits a GC pause / noisy neighbor / cold cache** and
> its responses go from 10 ms to 2,000 ms. B1 and B2 are fine. What does each
> policy do?

---

## 2. The policy ladder

### Round-robin: fair by count, blind to reality

Hand out backends in rotation: B1, B2, B3, B1, B2, B3… The scaffold even gives
you the cursor (`rr: AtomicUsize`). Distribution across a *healthy* pool is
perfectly even — which is exactly the trap in [CONCEPTS.md](../CONCEPTS.md):
**evenness on a healthy pool is the test everything passes.**

Against the slow-B3 scenario:

| | B1 | B2 | B3 (slow) |
|---|---|---|---|
| Share of requests | ⅓ | ⅓ | **⅓ — unchanged!** |
| Latency served | 10 ms | 10 ms | 2,000 ms |

Round-robin keeps feeding B3 its full third *because it never looks at
anything*. One-third of all requests are now 200× slower — your p66 is
ruined, never mind p99. Meanwhile B3's queue grows, making it slower still.
Round-robin is the floor: correct arithmetic, zero feedback.

(Pure **random** is the same story with extra variance: unlucky streaks hammer
one backend even when all are healthy.)

### Least-connections: feedback, at a price

Now use a live signal: track how many requests are currently **in flight** to
each backend, and pick the minimum. The scaffold maintains this signal for
you — [balancer.rs](../src/balancer.rs) has `incr_in_flight()` /
`decr_in_flight()` on an atomic counter (wiring them around the actual
forwarding is part of the work).

Why in-flight is such a good signal: a slow backend *accumulates* in-flight
requests (they arrive at the same rate but leave slower), so its count rises
within milliseconds of it degrading — no probe, no configuration, just
arithmetic. Against slow-B3: its in-flight climbs, least-conn stops picking
it, traffic shifts to B1/B2 automatically. 

But full least-connections has two costs:

1. **A scan.** Finding the true minimum touches every backend on every pick —
   O(pool) on the hot path.
2. **Herding.** This one is subtle and important. The signal is *slightly
   stale* (it changes the instant picks happen), and a deterministic rule
   applied to stale data makes every concurrent pick reach the **same**
   conclusion. Picture a fresh backend joining with 0 in-flight: for the next
   instant it is *everyone's* minimum, so an entire burst of concurrent
   requests piles onto it simultaneously — the "least loaded" node becomes the
   *most* loaded, overshooting, then the wave crashes somewhere else.
   Deterministic best-choice + stale signal = oscillation.

### P2C — power of two choices: the punchline

> **Sample two backends uniformly at random. Send the request to the
> less-loaded of the two.**

That's the entire algorithm, and it defuses both costs at once:

- **O(1)**, not O(pool): you look at exactly two backends no matter the pool size.
- **No herding:** concurrent picks sample *different* random pairs, so even
  with identical stale data they spread across the pool instead of stampeding
  a single minimum. The randomness isn't a compromise — it's load-bearing.

And it still crushes the slow-B3 scenario: B3 only receives traffic when it's
sampled *and* beats its opponent. As its in-flight count climbs, it loses
almost every comparison — traffic drains away from it nearly as decisively as
true least-connections, without the scan or the stampede.

### How much does the second choice buy? (verified numbers)

The classic balls-into-bins result (Azar et al.): throw n balls into n bins.
Simulated at n = 10,000 (I ran this — 20 trials, averaged):

| Strategy | Most-loaded bin (n = 10,000) |
|---|---|
| 1 random choice | **~6.8 balls** |
| 2 choices, pick emptier | **~3.1 balls** |

Halving the worst hotspot looks modest until you see the asymptotics: max
load drops from Θ(ln n / ln ln n) to ln ln n / ln 2 + O(1) — an
**exponential** improvement in the imbalance, bought by *one extra random
sample*. Going from two choices to three barely improves it further. Two is
the magic number; this is why Envoy's `LEAST_REQUEST` *is* P2C, and Finagle
and Linkerd ship P2C + EWMA as the default.

---

## 3. The second signal: EWMA latency

In-flight count has a blind spot: a backend that is **slow but not
saturated**. If traffic is light, even a degraded backend may show in-flight
≈ 0 most of the time — the count can't see slowness it isn't currently
queueing behind. Latency can.

An **EWMA** (exponentially weighted moving average) is a one-number latency
memory, updated on every completed response:

```
ewma ← α · latest_sample + (1 − α) · ewma        (0 < α ≤ 1)
```

Worked example, α = 0.2: a backend has been fast (ewma = 10 ms), then degrades
and starts returning 100 ms samples:

| Sample # | Sample | EWMA after |
|---|---|---|
| — | — | 10.0 ms |
| 1 | 100 ms | 28.0 ms |
| 2 | 100 ms | 42.4 ms |
| 3 | 100 ms | 53.9 ms |
| 4 | 100 ms | 63.1 ms |
| 5 | 100 ms | 70.5 ms |

Five responses in, the number has moved decisively — no history buffer, no
percentile math, just one atomic (`ewma_micros` in
[balancer.rs](../src/balancer.rs), stored as microseconds so it fits an
`AtomicU64`). α is the memory knob: bigger α reacts faster but jitters on
noise; smaller α is smoother but slower to notice trouble.

In P2C, the two signals compose naturally: compare the sampled pair on
in-flight (queue pressure now), with EWMA as the richer signal or tie-break —
catching slow-but-accepting backends that in-flight alone would miss. Exactly
how you combine them (and when you *update* the EWMA) is part of your V3
design.

---

## 4. The hot-path constraint and the health filter

Two more Done-when boxes shape the implementation:

**Cheap picks.** `pick()` runs on every request, concurrently. One global
`Mutex<State>` around the balancer serializes your entire gateway through a
single lock — measurable at high concurrency and completely unnecessary. The
scaffold's signals are all lock-free atomics (`AtomicUsize` in-flight,
`AtomicU64` ewma, the rr cursor); keeping the pick to a handful of atomic
loads is a design goal the SPEC asks you to *document*, not just achieve.
(Worth knowing: atomic ops that are relaxed loads are near-free; the expensive
thing is contended read-modify-write on one shared cache line — think about
which of those each policy needs per pick.)

**Never pick the ejected.** V4's health layer marks backends unavailable
(`Backend::is_available()` consults the circuit breaker). Every policy must
filter these *before* choosing — an open-circuit backend simply isn't in the
pool right now. Edge cases each policy must survive: all-but-one ejected,
and **all** ejected → `pick()` returns `None` → the caller maps it to
`AppError::NoHealthyBackend` (503, [error.rs](../src/error.rs)). Also think
about what exclusion does to round-robin's cursor arithmetic — it's a small
puzzle with a wrong-but-plausible answer.

---

## 5. What V3 leaves for you to decide

The interesting decisions, made visible but not made for you:

- How round-robin's rotation stays even when some backends are filtered out.
- What "less loaded" means for P2C: in-flight only? EWMA only? in-flight then
  EWMA? A combined score? (Finagle's answer is one option, not the only one.)
- Where the EWMA update lives, what α to use, and what a *timeout* should do
  to it (a 10 s timeout is latency data too — arguably the most important
  sample of all).
- How the three policies expose one interface (`pick()` per `self.policy`)
  while a test proves they make **different** choices on the same skewed trace.

The SPEC's proof then makes the payoff measurable: under one slow backend,
P2C's p99 beats round-robin's — the bench in `docs/10-benchmarks.md` is where
the whole ladder becomes numbers. `/quest` to start with acceptance tests;
`/hint` when stuck.

---

## Mental-model summary

| Concept | The model |
|---|---|
| Round-robin | Fair by count, blind to load — the floor, and fine until one backend degrades |
| In-flight count | Self-updating load signal: slow backends accumulate it automatically |
| Least-connections | Great signal, O(pool) scan + deterministic-choice herding |
| Herding | Deterministic best-pick + stale signal → everyone stampedes the same "empty" node |
| P2C | Two random samples, pick less loaded — O(1), herd-proof, exponentially better tail than random |
| EWMA | One-number latency memory (`α·new + (1−α)·old`); sees slow-but-not-saturated |
| Health filter | Ejected backends aren't in the pool; empty pool → `None` → 503 |
| Hot path | Atomic loads, not a global mutex — and document the choice |

## Where you'll build this

**Module:** [src/balancer.rs](../src/balancer.rs) — the `todo!()` in
`Balancer::pick()`, plus wiring the in-flight/EWMA accounting around the
forwarding path you built in V1. The policy enum (`RoundRobin` / `LeastConn` /
`P2c`) and all per-backend state already exist.

**This doc unlocks V3's Done-when criteria** ([SPEC.md](../SPEC.md) §V3):
balanced spread on a healthy pool, in-flight tracking with least-conn,
swappable policies that demonstrably diverge, exclusion of unhealthy backends,
a documented cheap hot path, and the P2C-vs-round-robin p99 bench under one
slow backend.
