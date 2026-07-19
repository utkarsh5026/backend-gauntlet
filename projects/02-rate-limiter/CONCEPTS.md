# Concept Bank — Project 02: Distributed Rate Limiter

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The token bucket *(V1 · `src/token_bucket.rs`)*

**The problem.** "Allow 100 requests per second" hides an ambiguity: does a client get to send 100 *at once* and then wait, or must they be spread out? Real traffic is bursty — a page load fires 15 API calls in 50 ms — so a limiter that only understands a smooth rate punishes normal behavior, and one that only understands totals lets abusers front-load.

**The idea.** A bucket holds up to `capacity` tokens (the burst allowance) and refills at `rate` tokens/sec (the sustained allowance). Each request spends tokens; empty bucket = denied. The two knobs are *independent* — that's the whole design. And refill is computed lazily from elapsed time on each check, so a million idle buckets cost nothing: no timers, no background tasks.

**In the wild:** AWS API throttling (they document bucket sizes literally), Stripe's limiter, nginx `limit_req` (leaky-bucket variant), Linux `tc` traffic shaping.

**You own it when you can explain:**
- [ ] Burst vs sustained rate as two independent knobs, with a concrete traffic pattern that needs them to differ.
- [ ] Lazy refill: how "tokens = min(capacity, tokens + elapsed × rate)" replaces a timer per bucket, and why that matters at a million keys.
- [ ] How to compute a truthful `retry_after` from the deficit and the refill rate — and why lying about it trains clients to hammer you.
- [ ] The precision trap: how repeated rounding on refill can slowly mint or destroy budget, and what representation avoids drift.
- [ ] Token bucket vs leaky bucket: which one shapes output to a smooth rate and which one admits bursts — they are not the same algorithm.

**Depth probes:**
- A client with `capacity=100, rate=10/s` is idle for an hour, then sends 200 requests instantly. Trace exactly what happens.
- Why is "check then decrement" as two steps a bug even in a single process with async tasks?

**Trap:** implementing refill with a background tick task. It works in a demo and collapses at scale — the number of buckets is unbounded, and the timer wakes for buckets nobody is using.

---

## 🧠 Card 2 — Sliding windows & the boundary burst *(V2 · `src/sliding_window.rs`)*

**The problem.** The simplest limiter — "count requests in the current minute, reset at :00" — has a hole you can drive 2× traffic through: send 100 requests at 11:00:59 and 100 more at 11:01:00. Both windows are individually legal; the 2-second stretch saw 200. Any attacker who can read a clock gets double your limit.

**The idea.** Make the window slide with `now` instead of snapping to wall-clock boundaries. The exact version (a log of timestamps) is precise but costs memory per request. The clever version (sliding window *counter*) keeps only two numbers — the current and previous fixed-window counts — and estimates the sliding count by weighting the previous window by how much of it still overlaps. O(1) memory, small bounded error.

**In the wild:** Cloudflare's published rate-limiter design is exactly the weighted-counter scheme; Redis-based limiters in most API gateways.

**You own it when you can explain:**
- [ ] The boundary-burst exploit, with the timeline drawn out, and why fixed windows structurally can't fix it.
- [ ] The sliding-window log: what it stores, what it costs, when that cost is acceptable.
- [ ] The counter's weighting formula in words: "previous count × overlap fraction + current count" — and the assumption (uniform arrival) that makes it approximate.
- [ ] The error bound of the approximation and why balanced traffic keeps it small.
- [ ] When you'd pick token bucket vs sliding window: burst-friendliness vs strict "never more than N in any window" guarantees.

**Depth probes:**
- An attacker knows you use the weighted counter. Can they still squeeze out more than the limit? How much?
- Why does the log version get *worse* under attack (memory grows with the attack traffic) — exactly when you need it most?

**Trap:** assuming "sliding window" is one algorithm. The log and the counter have wildly different cost profiles; naming which one you mean is the difference between an O(1) limiter and a memory leak.

---

## 🧠 Card 3 — Distributed atomicity: Redis + Lua *(V3 · `src/redis_limiter.rs`)*

**The problem.** One gateway instance is easy. Run N instances sharing one Redis, and the naive flow — GET the bucket, decide in Rust, SET it back — has a race: two instances read "1 token left" at the same instant, both approve, both write. You just admitted 2 requests on 1 token. This is TOCTOU (time-of-check-to-time-of-use), and no amount of care in your Rust code fixes it, because the race is *between processes*.

**The idea.** Move the entire read-modify-write into Redis itself as one Lua script. Redis executes scripts atomically on its single thread, so the check and the deduct are indivisible — the serialization point is the data's home, not a lock you bolt on. Load the script once, invoke it by SHA (`EVALSHA`), fall back to `EVAL` on `NOSCRIPT`.

**In the wild:** virtually every production Redis rate limiter (GitHub's, Shopify's, redis-cell), and the same pattern powers distributed locks and dedup — "push the decision to the store".

**You own it when you can explain:**
- [ ] TOCTOU with a two-instance interleaving diagram — where exactly the race window is.
- [ ] Why `INCR` alone can't do a token bucket (refill needs *read then compute then write*, not just increment).
- [ ] What makes Lua-in-Redis atomic (single-threaded execution) and what that costs (a slow script blocks *everything* — why scripts must be tiny).
- [ ] `EVAL` vs `EVALSHA` vs `NOSCRIPT`: the script-cache lifecycle across Redis restarts.
- [ ] Why every bucket key carries a TTL — the unbounded-keyspace leak without it.
- [ ] Fail-open vs fail-closed when Redis is unreachable: what each protects (availability vs abuse-resistance), why it must be configurable per caller, and who should decide.

**Depth probes:**
- Redis latency p99 jumps from 0.5 ms to 20 ms. What happens to your whole platform, and what's the mitigation (local L1 bucket, and what consistency it costs)?
- Could you shard buckets across N Redis nodes? What routing property must hold (same key → same node, always)?

**Trap:** reaching for a distributed lock. A lock around GET/SET "fixes" the race at 2× the round-trips and adds lock-expiry failure modes — the script *is* the lock, for free.

---

## ⚡ Rapid-fire round

- [ ] Why a limiter is judged at p99, not average — it taxes *every* request on the platform.
- [ ] gRPC deadlines: why a `Check` that can't answer in time should fail fast instead of queueing (deadline propagation, tail amplification).
- [ ] Why you limit per-identity *and* per-IP separately — the shared-NAT office and the credential-stuffing botnet are different attackers.
- [ ] `INVALID_ARGUMENT` vs `UNAVAILABLE` vs deadline exceeded — mapping failures so callers can react correctly.
- [ ] Why keys are hashed/truncated in logs (an API key in a log file is a leaked API key).
- [ ] What a gRPC health-check service is for (LB/k8s probes speak it natively).

## 🔗 Connects to

- The TOCTOU→atomicity lesson reappears as `FOR UPDATE SKIP LOCKED` in project 04 and isolation levels in project 18 — same race, three storage engines.
- Fail-open/fail-closed thinking returns in project 10's circuit breaker.
- This service is what project 01's `POST /api/links` would call — you built the thing you previously stubbed.
