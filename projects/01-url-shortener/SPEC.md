<!-- status:
state: active            # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 01 — URL Shortener + Analytics

> A URL shortener is the "hello world" of backend — but the *scalable* version is
> anything but. It's read-heavy (every redirect is a lookup), needs unique IDs
> without coordination, has to absorb bursty click traffic, and must not fall over
> when a link goes viral. That makes it the perfect first rung.

## What it does (the easy part)
- `POST /api/links` with a long URL → returns a short slug (e.g. `aZ3kQ`).
- `GET /{slug}` → `301`/`302` redirect to the original URL.
- `GET /api/links/{slug}/stats` → click count + recent analytics.
- API-key auth on the write/stats endpoints; redirects are public.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Distributed ID generation — *no DB sequences allowed*
A naive shortener does `INSERT ... RETURNING id` and base62-encodes the row id.
That couples ID generation to a single Postgres sequence — a scaling bottleneck and
a single point of failure. **Implement a Snowflake-style 64-bit ID generator** in
`src/url_shortener/id_gen.py`, then base62-encode it for the slug.

**Done when ALL true:**
- [x] IDs are generated **in-process** — zero DB/network round-trips on the create path.
- [x] IDs are **time-ordered**: for any two ids from one node, the later one is numerically greater.
- [x] Two generators with **different node ids never collide** — even when run concurrently.
- [x] A **same-millisecond burst** yields unique ids up to the sequence width, then waits for the next ms rather than colliding.
- [ ] **Clock moving backwards** has a defined, non-corrupting behavior (no duplicate ids, no panic-crash) — and it's documented.
- [x] Slug = base62(id): URL-safe characters only, and decodes back to the same id.

**Proof:** property tests for uniqueness under concurrency
(`tests/test_id_gen.py::test_concurrent_ids_are_unique`) + a `bench/` throughput
number (ids/sec, single node — `make bench`).

*Concept to internalize:* why coordination-free ID generation matters, and the
tradeoffs vs UUIDv4 (random, not sortable) and DB sequences (coordinated).
**Stretch:** custom vanity slugs with collision detection.

> **Python note.** The state that must move atomically is the pair
> `(last_timestamp, sequence)`. A lock-free CAS loop is the usual answer in a
> language with real parallel threads; here a `threading.Lock` is both the
> idiomatic and the faster tool, and it makes the backwards-clock case
> unambiguous — inside the lock, `now < last` can only mean the wall clock moved.
> Note also that the open box above bites *harder* in Python: a spin-wait on the
> event-loop thread stalls every other request, not just this one.

### V2. Cache-aside with stampede protection — *build the cache layer*
Redirects are the hot path and must not hit Postgres every time. Build the cache in
`src/url_shortener/cache.py` (and the read path it serves, `resolve.py`).

**Done when ALL true:**
- [x] **Cache-aside read path:** a cache *hit* touches Redis only — Postgres is never queried.
- [x] **Miss path** falls back to Postgres, then populates Redis so the next read is a hit.
- [x] **TTLs carry jitter** so a wave of entries written together don't all expire on the same tick.
- [x] **Negative caching:** an unknown slug is remembered (short TTL) so a 404 flood hits the DB at most once per window.
- [x] **Stampede invariant:** with **≥1k concurrent requests** racing on a single *just-expired* hot slug, Postgres sees **≤1 rebuild query** — not one per request.
- [x] Redis being **down degrades, not dies**: redirects still resolve from Postgres (defined fallback).

**Proof:** `tests/test_resolve.py` proves the DB is untouched on a hit and that a
broken cache degrades without back-filling;
`tests/test_cache.py::test_thousand_concurrent_misses_rebuild_once` proves ≤1
rebuild under a 1,000-way race; `bench/` shows redirect throughput **with vs
without** cache and the hit ratio under load; `docs/01-design.md` names the
stampede strategy and the failure mode you accepted (staleness? a brief wait?).

*Concept to internalize:* the difference between cache-aside, write-through, and
write-behind, and why stampedes are a real outage cause.

> **Python note.** The Rust version needed a `Mutex<HashMap>` plus a per-slot
> `Notify`, because threads could interleave the "is someone already rebuilding?"
> check with the "then I'm the leader" insert. On one event loop they cannot —
> there is no `await` between them — so the whole structure collapses to a
> `dict[str, asyncio.Future]`. Two things that are *not* optional: waiters must
> park with `asyncio.shield` (a bare `await future` means one client hanging up
> cancels the rebuild for everyone), and the Redis pool must be a
> **`BlockingConnectionPool`** — the default pool *raises* when exhausted, and
> that error reads as "cache unavailable", so a thundering herd would send itself
> straight to Postgres.

### V3. Async click ingestion — *don't block the redirect*
Recording analytics must never slow down the redirect. The handler hands the click
off and returns immediately. Build the ingestion path in
`src/url_shortener/ingest.py`.

**Done when ALL true:**
- [x] The redirect handler **returns without awaiting** any analytics DB write — redirect latency is independent of ingestion.
- [x] Click events flow through a **bounded** queue — there is no unbounded buffer anywhere on the redirect path.
- [x] **Overflow policy is explicit and enforced:** when the buffer is full the system does a *declared* thing (drop / block / shed) — and you can say which and why.
- [x] Clicks are **batched** into multi-row inserts (N rows or every T ms), not one `INSERT` per click — verifiable by counting statements.
- [x] **Graceful shutdown:** on SIGTERM, buffered clicks are flushed before exit — a clean shutdown loses nothing.

**Proof:** `tests/test_ingest.py::test_a_batch_is_exactly_one_statement` counts the
statements; `test_accept_sheds_when_the_queue_is_full` pins the overflow policy;
`tests/test_shutdown.py` pins the bounded final flush; `docs/01-design.md` records
the backpressure choice.

*Concept to internalize:* backpressure, batching, and trading exactness for throughput.
**Stretch:** approximate unique-visitor counts with a HyperLogLog instead of storing every event.

> **Python note.** `accept()` is deliberately a plain `def`, not a coroutine —
> if there is nothing to `await`, a handler cannot accidentally block a redirect
> on ingestion. The drain loop uses `asyncio.timeout_at` around `queue.get()` for
> the "or every T ms" half; do not reach for `sleep`-polling.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Redirect status is deliberate:** `GET /{slug}` returns the chosen code (`301`/`302`) — verifiable in the response. *(Proof: redirect test asserting the status.)*
- [ ] **Redirect choice is justified:** `docs/01-design.md` says *why* `301` vs `302` (it changes whether analytics ever see the second click). *(Proof: design doc.)*
- [ ] **`Cache-Control` / `ETag`** present where appropriate; a conditional request can get `304`.
- [ ] **Graceful shutdown** drains in-flight requests *and* flushes the click buffer on SIGTERM (no abrupt connection drops).

### Caching
- [x] Cache-aside implemented (V2) with sane, jittered TTLs.
- [x] Negative caching for unknown slugs.
- [ ] Stampede-protection strategy documented in `docs/01-design.md` with the tradeoff named.

### Security
- [ ] **API-key auth enforced** on write/stats routes (`src/url_shortener/auth.py`): a request without a valid key is rejected before the handler runs, and keys never appear in logs or error responses. *(Proof: `tests/test_routes.py::test_auth_runs_before_the_handler` + the credential-rejection cases.)*
- [ ] **Auth timing-safety is a documented decision:** `docs/01-design.md` states constant-time vs. plain set-membership and justifies the call. *(Proof: design doc.)*
- [ ] **Key at-rest story is stated:** `docs/01-design.md` records how keys are stored (plaintext in memory vs. hashed) and the tradeoff. *(Proof: design doc.)*
- [x] **URL validation:** submitted URLs are normalized and rejected on scheme not in the allowlist, `javascript:`, internal/loopback/link-local IPs (SSRF), or over-length — each with a test. *(`tests/test_url_validate.py`; SSRF covers IPv4, IPv6 literals, and IPv4-mapped IPv6.)*
- [x] **No SQL injection:** every query is parameterized (`$1` placeholders bound by asyncpg) — zero string-concatenated SQL, including the generated multi-row INSERT, whose placeholders come from the batch *length*.
- [x] Per-key rate limiting on `POST /api/links` (a taste of project 02).

### Observability
- [x] One structured log line per request with a request id (via `common-telemetry`'s `RequestIdMiddleware`), echoed back as `x-request-id`.
- [x] Each redirect logs **slug, cache hit/miss, and latency** as structured fields.
- [x] Counter metrics exported at `/metrics`: **redirects, cache hit ratio, ingestion queue depth.** *(Proof: `tests/test_routes.py::test_metrics_endpoint_renders_the_graded_metrics`.)*

### Python craft
This axis is the day-job curriculum — it is graded, not decoration.

- [x] **pyright strict passes clean** — every `# pyright: ignore` carries a justifying comment.
- [ ] **No blocking call on the event loop** — runs clean under `PYTHONASYNCIODEBUG=1`; any sync I/O is in a thread/process pool deliberately.
- [ ] **Bounded pool sized on purpose** — the Postgres pool size, the Redis pool size, and the worker count tuned *together*, with the reasoning in the design doc.
- [x] **Graceful shutdown** drains in-flight requests on SIGTERM via the FastAPI lifespan. *(Proof: `tests/test_shutdown.py`; uvicorn stops accepting, then the lifespan's `finally` drains within a budget.)*
- [ ] **Profile committed** — a `py-spy` flamegraph (`make profile`) and a `memray` run in `docs/01-benchmarks.md`, naming the top bottleneck.

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/01-benchmarks.md`.
3. `docs/01-design.md` records the three decisions the SPEC grades: **ID scheme,
   stampede strategy, backpressure choice** (+ the auth timing-safety call).
4. **You know *why* the numbers are what they are** — a `py-spy` flamegraph and a
   `memray` run in `docs/01-benchmarks.md`, naming the top bottleneck. Numbers
   alone do not close this: a throughput figure with no explanation is a
   measurement, not an understanding.
5. `make verify` is green (ruff format + ruff check + pyright strict + pytest);
   no `NotImplementedError` remains on a checked path.

## 🐉 Boss fight — The Thundering Herd

> A link you shortened hits the front page of Hacker News. The cache entry for it
> expired **one second ago**. Thousands of clients are now racing for the same cold
> key — and every one of them is happy to stampede into Postgres if you let them.

**Arena:** `bench/` load test (`oha` or `k6`) against the service run for real
(`make run`, uvicorn on uvloop — *not* `--reload`) with Postgres + Redis up. Two
runs: cache on vs. cache bypassed, plus one cold-key stampede scenario.

**The boss falls when ALL true:**
- [ ] ≥ **5,000 redirects/sec** sustained for 60s on a hot-key workload.
- [ ] **p99 ≤ 20ms** during that run.
- [ ] 1,000 concurrent requests for the **same cold key** reach Postgres as **≤ 1 query**
  (prove it with the DB query counter / logs, not vibes).
- [ ] Cache hit ratio **≥ 95%** on the mixed workload, and the cache-on run beats
  cache-bypassed by **≥ 5×** throughput.

**Proof:** methodology + before/after numbers in `docs/01-benchmarks.md`
(hardware noted, commands reproducible via `bench/`).

> **The numbers are not scaled down for Python.** They were set by what the
> workload demands, not by what a runtime can comfortably reach, and moving the
> goalposts would delete the finding. Where CPython cannot get there, *the gap is
> the result*: record where it topped out and **why** — GIL contention? GC pauses?
> per-request allocation? a blocking call that slipped onto the loop? one uvicorn
> worker where the fix is `N` behind a socket? Naming the true bottleneck is worth
> more than hitting the number, and `make bench` already shows the shape of this:
> the id generator's 4,096/ms ceiling is a property of the bit layout, so the
> distance between that ceiling and what CPython actually reaches is a pure,
> readable measure of interpreter overhead.

## Suggested order of attack
1. Get the boring path working: `POST` + `GET` redirect straight to Postgres (no cache).
2. Add the Snowflake ID generator (V1).
3. Add the Redis cache-aside layer, then make it stampede-safe (V2).
4. Add async click ingestion (V3).
5. Add auth + URL validation + rate limiting (security).
6. Benchmark, profile, document, tune.

## Run the dependencies
```bash
make deps                   # postgres + redis (docker compose)
cp .env.example .env        # then fill in values
make migrate                # apply migrations (no sqlx-cli — it's a Python runner)
make run                    # uvicorn on uvloop
make demo                   # deps + migrate + serve the browser dashboard
```
