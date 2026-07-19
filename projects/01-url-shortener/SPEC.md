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
`src/id_gen.rs`, then base62-encode it for the slug.

**Done when ALL true:**
- [x] IDs are generated **in-process** — zero DB/network round-trips on the create path.
- [x] IDs are **time-ordered**: for any two ids from one node, the later one is numerically greater.
- [x] Two generators with **different node ids never collide** — even when run concurrently.
- [x] A **same-millisecond burst** yields unique ids up to the sequence width, then waits for the next ms rather than colliding.
- [ ] **Clock moving backwards** has a defined, non-corrupting behavior (no duplicate ids, no panic-crash) — and it's documented.
- [x] Slug = base62(id): URL-safe characters only, and decodes back to the same id.

**Proof:** property tests for uniqueness under concurrency (`prop_concurrent_ids_are_unique`)
+ a `bench/` throughput number (ids/sec, single node).

*Concept to internalize:* why coordination-free ID generation matters, and the
tradeoffs vs UUIDv4 (random, not sortable) and DB sequences (coordinated).
**Stretch:** custom vanity slugs with collision detection.

### V2. Cache-aside with stampede protection — *build the cache layer*
Redirects are the hot path and must not hit Postgres every time. Build the cache in
`src/cache.rs`.

**Done when ALL true:**
- [x] **Cache-aside read path:** a cache *hit* touches Redis only — Postgres is never queried.
- [x] **Miss path** falls back to Postgres, then populates Redis so the next read is a hit.
- [x] **TTLs carry jitter** so a wave of entries written together don't all expire on the same tick.
- [x] **Negative caching:** an unknown slug is remembered (short TTL) so a 404 flood hits the DB at most once per window.
- [x] **Stampede invariant:** with **≥1k concurrent requests** racing on a single *just-expired* hot slug, Postgres sees **≤1 rebuild query** — not one per request.
- [x] Redis being **down degrades, not dies**: redirects still resolve from Postgres (defined fallback).

**Proof:** integration tests proving DB is untouched on hit + ≤1 rebuild under a
concurrent race; `bench/` showing redirect throughput **with vs without** cache and
the hit ratio under load; `docs/01-design.md` names the stampede strategy and the
failure mode you accepted (staleness? a brief wait?).

*Concept to internalize:* the difference between cache-aside, write-through, and
write-behind, and why stampedes are a real outage cause.

### V3. Async click ingestion — *don't block the redirect*
Recording analytics must never slow down the redirect. The handler hands the click
off and returns immediately. Build the ingestion path in `src/ingest.rs`.

**Done when ALL true:**
- [x] The redirect handler **returns without awaiting** any analytics DB write — redirect latency is independent of ingestion.
- [x] Click events flow through a **bounded** channel — there is no unbounded queue anywhere on the redirect path.
- [x] **Overflow policy is explicit and enforced:** when the buffer is full the system does a *declared* thing (drop / block / shed) — and you can say which and why.
- [x] Clicks are **batched** into multi-row inserts (N rows or every T ms), not one `INSERT` per click — verifiable by counting statements.
- [x] **Graceful shutdown:** on SIGTERM, buffered clicks are flushed before exit — a clean shutdown loses nothing.

**Proof:** a test showing redirect p99 is unaffected while the click buffer is
saturated; statement-count assertion proving batching; `docs/01-design.md` records
the backpressure choice.

*Concept to internalize:* backpressure, batching, and trading exactness for throughput.
**Stretch:** approximate unique-visitor counts with a HyperLogLog instead of storing every event.

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
- [ ] **API-key auth enforced** on write/stats routes (`src/auth.rs`): a request without a valid key is rejected before the handler runs, and keys never appear in logs or error responses. *(Proof: `src/auth.rs` middleware + reject tests.)*
- [ ] **Auth timing-safety is a documented decision:** `docs/01-design.md` states constant-time vs. `HashSet`-lookup and justifies the call. *(Proof: design doc.)*
- [ ] **Key at-rest story is stated:** `docs/01-design.md` records how keys are stored (plaintext in memory vs. hashed) and the tradeoff. *(Proof: design doc.)*
- [x] **URL validation:** submitted URLs are normalized and rejected on scheme not in the allowlist, `javascript:`, internal/loopback/link-local IPs (SSRF), or over-length — each with a test. *(`src/url_validate.rs` tests; SSRF covers IPv4 + IPv6 literals via the typed `Host`.)*
- [x] **No SQL injection:** every query is `sqlx` compile-time-checked (`query!`) — zero string-concatenated SQL.
- [x] Per-key rate limiting on `POST /api/links` (a taste of project 02).

### Observability
- [x] `tracing` span per request (via `common-telemetry`), with a request id. *(`common_telemetry::make_request_span` wired into the `TraceLayer`; nested handler spans inherit the id.)*
- [x] Each redirect logs **slug, cache hit/miss, and latency** as structured fields.
- [x] Counter metrics exported at `/metrics`: **redirects, cache hit ratio, ingestion queue depth.** *(Proof: `metrics_endpoint_renders_recorded_counters`.)*

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/01-benchmarks.md`.
3. `docs/01-design.md` records the three decisions the SPEC grades: **ID scheme,
   stampede strategy, backpressure choice** (+ the auth timing-safety call).
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p url-shortener` are green;
   no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Thundering Herd

> A link you shortened hits the front page of Hacker News. The cache entry for it
> expired **one second ago**. Thousands of clients are now racing for the same cold
> key — and every one of them is happy to stampede into Postgres if you let them.

**Arena:** `bench/` load test (`oha` or `k6`) against a **release build**
(`cargo run --release`) with Postgres + Redis up. Two runs: cache on vs. cache
bypassed, plus one cold-key stampede scenario.

**The boss falls when ALL true:**
- [ ] ≥ **5,000 redirects/sec** sustained for 60s on a hot-key workload.
- [ ] **p99 ≤ 20ms** during that run.
- [ ] 1,000 concurrent requests for the **same cold key** reach Postgres as **≤ 1 query**
  (prove it with the DB query counter / logs, not vibes).
- [ ] Cache hit ratio **≥ 95%** on the mixed workload, and the cache-on run beats
  cache-bypassed by **≥ 5×** throughput.

**Proof:** methodology + before/after numbers in `docs/01-benchmarks.md`
(hardware noted, commands reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: `POST` + `GET` redirect straight to Postgres (no cache).
2. Add the Snowflake ID generator (V1).
3. Add the Redis cache-aside layer, then make it stampede-safe (V2).
4. Add async click ingestion (V3).
5. Add auth + URL validation + rate limiting (security).
6. Benchmark, document, tune.

## Run the dependencies
```bash
docker compose up -d        # postgres + redis
cp .env.example .env        # then fill in values
sqlx migrate run            # apply migrations (install: cargo install sqlx-cli)
cargo run -p url-shortener
```
