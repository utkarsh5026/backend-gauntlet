<!-- status:
state: paused            # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 02 — Distributed Rate Limiter

> A rate limiter looks trivial: "allow N requests per second". The trap is that
> the *interesting* version runs on **many** gateway instances at once, all
> sharing one source of truth, and has to make a correct allow/deny decision in
> well under a millisecond on the hot path — without letting two concurrent
> requests both "see" the last remaining token. It's a small algorithm wrapped in
> a hard distributed-systems and concurrency problem. That's the rung.

## What it does (the easy part)
- A gRPC service with a single hot method: `Check(key, cost) → {allowed, remaining, retry_after}`.
- `key` is whatever you limit on: an API key, user id, or client IP.
- A `Peek(key)` method that reports state **without** consuming budget.
- Limits are configured per key (a global default to start; per-tier later).
- It is the thing that *other* services (e.g. project 01's `POST /api/links`) call
  before admitting a request.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it. The criteria describe *what the system must do*, never *how*;
> figuring out the how is the point. A box only flips to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Token bucket — *the algorithm, from scratch, no background timer*
In `src/rate_limiter/token_bucket.py`, implement a token bucket as a pure,
in-process class with no I/O:
- A bucket has a **capacity** (burst) and a **refill rate** (sustained tokens/sec).
- Crucially: **refill lazily on read**, not with a background task. On each
  `try_acquire`, compute how many tokens have accrued since `last_refill` from
  elapsed time, cap at capacity, then try to deduct `cost`.
- On deny, compute a truthful `retry_after`: how long until enough tokens exist.
- Watch the precision: `float` tokens accumulate representation error, `int()`
  truncates and `round()` banks — never let any of them manufacture or destroy
  budget over time.
- Time is injected as a `float` of `time.monotonic()` seconds, so a test can
  simulate an hour of refills instantly and a clock step can't rewrite history.

**Done when ALL true:**
- [ ] The bucket exposes two **independent** knobs: capacity (burst) and refill rate (sustained tokens/sec).
- [ ] Refill is **lazy (computed on read)** — there is no background timer or task per bucket.
- [ ] A burst up to capacity is admitted at once; sustained traffic then settles to the refill rate (shown over a timed sequence).
- [ ] A denied `Check` returns a **truthful `retry_after`** — waiting exactly that long makes the next `Check` succeed.
- [ ] Token accounting **neither manufactures nor loses budget** across many refills — no rounding drift (property-tested).

**Proof:** unit tests for burst-then-throttle, plus a `hypothesis` property test that
a random sequence of costs and time steps never drifts from the configured long-run rate.

*Concept to internalize:* rate vs burst as two independent knobs, and why lazy
refill (compute-on-read) beats a timer-per-bucket at scale.

### V2. Sliding window — *fix the fixed-window boundary burst*
A fixed-window counter ("≤100 per minute, reset on the minute") allows a **2×
burst** across a window boundary: 100 at 11:00:59 and 100 at 11:01:00. In
`src/rate_limiter/sliding_window.py`, implement a sliding window that closes that hole:
- **Sliding window log** — store request timestamps in a `collections.deque`,
  `popleft()` the ones older than the window, count the rest. Exact, but memory
  grows with traffic.
- **Sliding window counter** — keep the current + previous fixed-window counts and
  weight the previous one by how much of it still overlaps `now`. Approximate,
  O(1) memory: two integers per key no matter the traffic.
- Implement at least the counter; understand the log well enough to explain the
  tradeoff.

**Done when ALL true:**
- [ ] The **fixed-window boundary burst** is demonstrated (a test shows a fixed counter admits ~2× across a boundary) — and the sliding implementation does **not**.
- [ ] The sliding-window **counter** is implemented with **O(1) memory** (current + weighted previous), independent of traffic volume.
- [ ] Decisions stay within a documented error bound of the exact log over the window.
- [ ] You can state the log-vs-counter exactness/cost tradeoff (in `docs/02-design.md`).

**Proof:** a test driving traffic across a window boundary that asserts the sliding limiter holds the rate the fixed one breaks.

*Concept to internalize:* the fixed-window boundary spike, and the exactness↔cost
tradeoff between the log and the counter.

### V3. Distributed atomicity — *Redis + Lua, no races across instances*
A single-process limiter is easy; **N** gateway instances sharing one Redis is
where it gets real. In `src/rate_limiter/redis_limiter.py`, move the state into
Redis and make each decision atomic:
- The naive "read state, decide, write state" from Python is a **TOCTOU race**:
  two instances both read the last token and both allow. `INCR` alone can't
  express a token bucket's refill.
- Push the whole read-modify-write into **one Lua script** that runs atomically
  inside Redis (`EVALSHA` by hash, falling back to `EVAL` on `NOSCRIPT`). The
  script does the refill math and the deduct, and returns the decision.
- Take the timestamp **inside the script** (`redis.call("TIME")`): N instances
  have N slightly different clocks, and a refill that depends on whose clock
  asked is not a shared limiter.
- Set a TTL on each key so idle buckets self-evict (don't leak memory in Redis).
- Decide the failure mode: when Redis is unreachable, do you **fail open** (allow)
  or **fail closed** (deny)? Make it explicit and configurable — and be careful
  which exceptions count as "unreachable": a `ResponseError` from a bug in your
  Lua is not an outage, and failing open on it hides the bug behind an
  unlimited endpoint.

**Done when ALL true:**
- [ ] State lives in Redis and the read-modify-write decision runs as **one atomic Lua script** — no TOCTOU window.
- [ ] The script is loaded once and called by **SHA (`EVALSHA`)**, falling back to `EVAL` on `NOSCRIPT`.
- [ ] **N concurrent instances never over-admit** past the limit — proven by a concurrency test hammering one key.
- [ ] Idle keys **self-evict** via TTL — no unbounded key growth in Redis.
- [ ] Redis-unreachable behavior is an **explicit, configurable** fail-open/fail-closed policy — and it's tested.

**Proof:** a concurrency test (many `asyncio.gather`-ed clients against one key) asserting no over-admission; a test of the fail-open/closed path with Redis down; bench numbers in `docs/02-benchmarks.md`.

*Concept to internalize:* why read-modify-write must be atomic under concurrency,
and how server-side scripts give you atomicity without a lock round-trip.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] gRPC service over HTTP/2 with a `.proto` contract (`grpcio` + `grpc.aio`).
- [ ] Honor client **deadlines/timeouts**: a `Check` that can't beat
  `context.time_remaining()` should fail fast, not pile up.
- [ ] Wire a gRPC **health check** service (`grpc.health.v1`) and (stretch) server
  reflection so `grpcurl` can introspect it.
- [ ] Map errors to correct gRPC status codes (`UNAVAILABLE`, `INVALID_ARGUMENT`).

### State & caching
- [ ] Redis holds the shared state (V3); load the Lua script once and call it by
  SHA (`EVALSHA`), with a fallback to `EVAL` on `NOSCRIPT`.
- [ ] TTLs so idle keys expire; no unbounded key growth.
- [ ] (Stretch) An in-process L1 cache / local token bucket in front of Redis to
  cut round-trips for very hot keys — and reason about the consistency cost.

### Security / abuse protection
- [ ] Sane keying: limit on identity (API key/user), and separately on IP, so one
  noisy tenant can't exhaust a shared bucket.
- [ ] Explicit **fail-open vs fail-closed** policy when the backend is down.
- [ ] Reject malformed requests (empty key, absurd `cost`) with `INVALID_ARGUMENT`.
- [ ] Never log full keys/secrets — hash or truncate in logs.

### Observability
- [ ] A structured log line / span per `Check` with fields: key (hashed), decision,
  remaining, backend latency.
- [ ] Counter metrics: allowed vs denied, Redis errors, script cache hits.
- [ ] A histogram of decision latency (this is a hot-path service — p99 matters).

### Python & runtime
- [ ] **`pyright` strict passes clean** — every `# type: ignore` / `# pyright: ignore` carries a comment justifying it.
- [ ] **No blocking call on the event loop:** the server runs clean under `PYTHONASYNCIODEBUG=1`, and any sync/CPU-bound work is moved to a thread or process pool *deliberately*, with the reason recorded.
- [ ] **Bounded pools sized on purpose:** `REDIS_MAX_CONNECTIONS` and `MAX_CONCURRENT_RPCS` are tuned *together* — an unbounded pool turns a slow Redis into unbounded memory, too small a one turns it into an invisible queue. The reasoning is in `docs/02-design.md`.
- [ ] **Graceful shutdown** on SIGTERM: health flips to `NOT_SERVING` *before* the drain, in-flight RPCs finish within the grace period, and the Redis pool is closed.
- [ ] **Profile committed:** a `py-spy` flamegraph and a `memray` run in `docs/02-benchmarks.md`, naming the top bottleneck on the hot path.

---

## Cross-cutting scale skills
- Concurrency correctness: prove (with a test) that concurrent `Check`s never
  over-admit past the limit.
- Hot-path latency discipline: one network round-trip budget, measured at p99.
- Graceful degradation: defined, tested behavior when Redis is unavailable.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its **Proof** artifact).
2. A `bench/` load test (`ghz`/`k6` against the gRPC endpoint) reporting **decision
   throughput and p50/p99 latency**, the allow/deny split under sustained
   overload, and a measured fail-open path with Redis killed. Numbers in
   `docs/02-benchmarks.md`.
3. A short `docs/02-design.md`: which algorithm you shipped and why, the Lua
   atomicity argument, your key/TTL scheme, the pool/concurrency sizing, and the
   fail-open/closed decision.
4. `make verify` is green — `ruff` clean, `pyright` **strict** with zero errors,
   and `pytest` passing; no `NotImplementedError` remains on a checked path.
5. A **profile** is committed: a `py-spy` flamegraph and a `memray` run in
   `docs/02-benchmarks.md`, naming the top bottleneck. Numbers alone don't close
   this — you have to know *why* they are what they are. On a hot-path service in
   CPython this is where you find out what the per-request cost actually buys:
   protobuf parsing, the Redis round-trip, or the interpreter itself.

## Suggested order of attack
1. Confirm gRPC is talking: `make dev`, then `grpcurl -plaintext localhost:50051 list`
   (reflection is already wired, so no `-proto` flag needed).
2. Implement the in-process token bucket (V1) and unit-test it hard — no I/O, no
   Redis, just the arithmetic and injected time.
3. Add the sliding window (V2); compare its boundary behavior against the bucket.
4. Move state into Redis with an atomic Lua script (V3); make it correct across
   two instances.
5. Add deadlines, the fail-open policy, and observability.
6. Benchmark, profile, document, tune.

## Run the dependencies
```bash
make setup                  # .env from .env.example
make dev                    # sync + Redis + run the server
make smoke                  # gRPC Peek probe over reflection
make health                 # GET /healthz on the admin port

# or by hand, once Check returns something:
#   grpcurl -plaintext -d '{"key":"user-1","cost":1}' localhost:50051 ratelimit.v1.RateLimiter/Check
```
