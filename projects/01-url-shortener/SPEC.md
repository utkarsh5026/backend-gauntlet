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

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Distributed ID generation — *no DB sequences allowed*
A naive shortener does `INSERT ... RETURNING id` and base62-encodes the row id.
That couples ID generation to a single Postgres sequence — a scaling bottleneck and
a single point of failure. **Implement a Snowflake-style 64-bit ID generator** in
`src/id_gen.rs`:
- time-ordered, monotonic, generated *in-process* with no DB round-trip,
- encodes a worker/node id so multiple instances never collide,
- handles the same-millisecond case (sequence counter) and clock going backwards.
- Then base62-encode it for the slug.
- **Stretch:** also support custom vanity slugs with collision detection.

*Concept to internalize:* why coordination-free ID generation matters, and the
tradeoffs vs UUIDv4 (random, not sortable) and DB sequences (coordinated).

### V2. Cache-aside with stampede protection — *build the cache layer*
Redirects are the hot path and must not hit Postgres every time. In `src/cache.rs`:
- cache-aside: look up Redis first, fall back to Postgres, populate Redis.
- **Cache stampede / thundering herd:** when a hot slug expires, thousands of
  concurrent requests must NOT all hammer Postgres. Implement a guard so only one
  request rebuilds the entry while the rest wait or serve slightly-stale data.
  (Research: single-flight, probabilistic early expiration, or a short lock.)
- Negative caching: cache "this slug does not exist" so 404 floods don't hit the DB.

*Concept to internalize:* the difference between cache-aside, write-through, and
write-behind, and why stampedes are a real outage cause.

### V3. Async click ingestion — *don't block the redirect*
Recording analytics must never slow down the redirect. The redirect handler should
hand the click event off and return immediately.
- Use a bounded channel + background task to batch-insert click events.
- **Bounded** is the point: decide what happens when analytics can't keep up
  (drop? block? shed load?). Document your backpressure choice.
- **Stretch:** approximate unique-visitor counts with a HyperLogLog instead of
  storing every event.

*Concept to internalize:* backpressure, batching, and trading exactness for throughput.

---

## Horizontal checklist (the backend fundamentals)

### Protocols
- [ ] Correct HTTP semantics: `301` vs `302` (and why it matters for analytics).
- [ ] `Cache-Control` / `ETag` headers on responses where appropriate.
- [ ] Graceful shutdown: drain in-flight requests + flush the click buffer on SIGTERM.

### Caching
- [ ] Cache-aside implemented (V2) with sane TTLs.
- [ ] Negative caching for unknown slugs.
- [ ] Document your stampede-protection strategy in `docs/`.

### Security
- [ ] API-key auth middleware on write/stats routes (`src/auth.rs`) — constant-time
      comparison, keys hashed at rest, never logged.
- [ ] Validate + normalize submitted URLs (reject `javascript:`, internal IPs/SSRF,
      enforce scheme allowlist, cap length).
- [ ] Use `sqlx` compile-time-checked queries (no string-concatenated SQL → no injection).
- [ ] Per-key rate limiting on `POST /api/links` (a taste of project 02).

### Observability
- [ ] `tracing` spans on every request (use `common-telemetry`).
- [ ] Log slug, cache hit/miss, and latency per redirect (structured fields).
- [ ] Counter metrics: redirects, cache hit ratio, ingestion queue depth.

---

## Definition of done
1. All vertical + horizontal boxes checked.
2. A `bench/` load test (`oha`/`k6`) showing redirect throughput **with vs without**
   the cache, and the cache hit ratio under load. Put numbers in `docs/01-benchmarks.md`.
3. A short `docs/01-design.md`: your ID scheme, stampede strategy, and backpressure choice.

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
