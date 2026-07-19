# Concept Bank — Project 01: URL Shortener + Analytics

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — Coordination-free ID generation *(V1 · `src/id_gen.rs`)*

**The problem.** Every new link needs a unique ID. The obvious answer — let Postgres hand them out with `INSERT ... RETURNING id` — makes every single create wait on one database, and that sequence becomes both your throughput ceiling and a single point of failure. At 10 rps nobody notices; at 10k rps the sequence *is* the bottleneck, and you can't shard it without collisions.

**The idea.** Make uniqueness come from *structure* instead of *agreement*: pack a timestamp, a node id, and a per-millisecond counter into one 64-bit integer. Two generators can never collide because they differ in at least one field — no network round-trip, no shared counter, no coordination.

**In the wild:** Twitter Snowflake (the original), Discord and Instagram IDs, Sonyflake; ULID/KSUID are string-flavored cousins.

**You own it when you can explain:**
- [ ] The bit layout of a Snowflake ID and what capacity each field buys (how many years, how many nodes, how many IDs per ms).
- [ ] Why the IDs are *time-sortable* and what that's worth downstream (B-tree index locality, cursor pagination, "when was this created" for free).
- [ ] The full tradeoff triangle: Snowflake vs UUIDv4 vs DB sequence — coordination, sortability, guessability, storage size.
- [ ] What happens in a same-millisecond burst when the sequence bits run out, and why "wait for the next ms" beats "collide".
- [ ] The clock-skew problem: what NTP stepping the clock backwards can do to a time-based generator, and two defensible responses to it.
- [ ] Why base62 for the public slug (URL-safe alphabet, no padding) and how it round-trips to the same integer.

**Depth probes:**
- Your service now runs 2,000 pods — node-id bits are exhausted. What are your options?
- A product manager wants "unguessable" links. Does Snowflake give you that? What would?
- Why do sequential-ish IDs *help* a database index where random UUIDs hurt it (page splits, WAL churn)?

**Trap:** thinking uniqueness is the hard part. Uniqueness is easy — the interesting engineering is what the ID's *shape* costs or buys everywhere else (indexes, sorting, privacy, sharding).

---

## 🧠 Card 2 — Cache-aside & stampede protection *(V2 · `src/cache.rs`)*

**The problem.** Every redirect is a read, and reads outnumber writes maybe 1000:1. Hitting Postgres per redirect melts it. So you put Redis in front — and now you own the classic cache problems: what happens on a miss, what happens when many misses happen *at once*, and what happens when Redis dies. The nightmare scenario: a viral link's cache entry expires and 5,000 in-flight requests all miss simultaneously — every one of them queries Postgres for the same row. That's a **stampede**, and it has taken down real sites.

**The idea.** Cache-aside: the app reads cache first, falls back to the DB on miss, then populates the cache. Stampede protection makes the rebuild a *single-flight* event: however many requests race on a cold key, at most one touches the database; the rest wait or serve stale.

**In the wild:** essentially every read-heavy service; the Facebook memcache paper ("leases") is the canonical stampede writeup; Cloudflare/CDNs call it request coalescing.

**You own it when you can explain:**
- [ ] Cache-aside vs write-through vs write-behind — draw the data flow of each and name a workload where each wins.
- [ ] The stampede mechanics: why expiry (not cold start) is the usual trigger, and why the damage scales with concurrency × rebuild cost.
- [ ] Two stampede defenses and the failure mode each accepts (a lock/single-flight → brief added latency; serve-stale/probabilistic early refresh → staleness).
- [ ] TTL jitter: what synchronized expiry does to a warm cache and how a few percent of randomness prevents it.
- [ ] Negative caching: why a 404-flood (scanners, typos) is a DB DoS without it, and why its TTL must be short (the slug might be created a second later).
- [ ] The Redis-down story: what "degrade, not die" costs (every request pays the DB price) and what protects the DB in that mode.

**Depth probes:**
- When is cache-aside *wrong*? (Write-heavy keys, read-your-writes requirements, large fan-out invalidation.)
- What's the difference between a stampede on one hot key and a cold-cache restart of the whole fleet? Different defenses?

**Trap:** believing a cache is "just an optimization". Once traffic is sized for the cache being there, the cache is *load-bearing infrastructure* — its failure modes are your outage modes.

---

## 🧠 Card 3 — Async ingestion, backpressure & batching *(V3 · `src/ingest.rs`)*

**The problem.** Every redirect should record a click — but an analytics INSERT on the redirect path means users wait on your analytics. Hand the click to a queue and return immediately, and you've created a new problem: what if clicks arrive faster than the writer drains them? An unbounded queue answers "buffer forever" — which is an OOM with a delay on it.

**The idea.** A *bounded* channel between the handler and a background writer. Bounded means full is possible, so you must pick an explicit overflow policy — drop, block, or shed — and that choice is a product decision (losing a click is fine; losing a payment is not). The writer batches: N clicks per INSERT instead of one, trading per-row latency for order-of-magnitude throughput.

**In the wild:** every telemetry/analytics SDK (statsd, Segment), Kafka producers (linger.ms + batch.size are exactly this knob), log shippers.

**You own it when you can explain:**
- [ ] Why the redirect must not *await* the analytics write — the difference between handing off and blocking, in latency terms.
- [ ] Why "bounded" is the entire point of the channel — what an unbounded queue in front of a slow consumer always eventually does.
- [ ] The three overflow policies (drop / block / shed) and which one fits click analytics, and *why*.
- [ ] Where multi-row INSERT savings actually come from: round-trips, per-statement parsing/planning, WAL flushes.
- [ ] The graceful-shutdown contract: what SIGTERM must flush so a clean deploy loses zero clicks, and why a crash may still lose some (and that's accepted).
- [ ] The general principle: you traded *exactness* (some clicks may drop under overload) for *throughput and latency* — and you can say where that trade is written down.

**Depth probes:**
- How would you make click counts *approximately* right at massive scale without storing every event? (HyperLogLog, sampling.)
- The buffer is chronically 90% full. What do you look at first — producer rate, batch size, flush interval, or DB latency — and why?

**Trap:** setting the buffer "big enough to never fill". A bigger buffer only converts fast failure into slow failure plus memory pressure; the overflow policy is the real design.

---

## ⚡ Rapid-fire round *(horizontals — one-liner answers you should have loaded)*

- [ ] 301 vs 302: which one browsers cache, and why a cached permanent redirect means your analytics never see that user again.
- [ ] How `ETag` + `If-None-Match` → `304` saves bandwidth but not backend work.
- [ ] SSRF: how an open URL-submission endpoint becomes a proxy into your internal network (`http://169.254.169.254/…`), and why the validator must reject loopback/link-local/private IPs — for IPv6 too.
- [ ] Why `javascript:` URLs must be rejected (stored XSS via redirect).
- [ ] What a timing attack on `key == stored_key` looks like and when constant-time comparison genuinely matters.
- [ ] Why `sqlx::query!` makes SQL injection structurally impossible rather than "sanitized".
- [ ] What a per-request tracing span with a request id buys you the first time production misbehaves.

## 🔗 Connects to

- The stampede idea returns as **single-flight at the edge** in project 16 (V3) — same problem, on video segments.
- The bounded-channel/backpressure idea is the spine of projects 03 (slow WebSocket consumers) and 05 (pipeline backpressure).
- Per-key rate limiting here is a teaser for project 02.
