# 01 — URL Shortener: Design Decisions

> Decision log for the choices the SPEC grades on. Each section is a mini
> decision record: **Context** (the forces) → **Options** → **Decision** (what
> you chose) → **Why** (the tradeoff you accepted). Fill the blanks as you build;
> raw benchmark numbers live in [`01-benchmarks.md`](./01-benchmarks.md), not here.

---

## V1 — Distributed ID scheme

**Context.** Slugs come from a Snowflake-style 64-bit ID (no DB sequence). Need
time-ordered, in-process, collision-free across nodes.

**Bit layout.** _(fill in your split — should sum to 63 usable bits)_

| Field      | Bits | Range / meaning      |
|------------|-----:|----------------------|
| timestamp  |   __ | ms since epoch `___` |
| node id    |   __ | up to `___` nodes    |
| sequence   |   __ | `___` ids / ms / node |

- **Custom epoch:** `____________`  — _why not the Unix epoch?_
- **Clock-going-backwards policy:** _(reject? wait? borrow from sequence?)_ `____`
- **Same-millisecond exhaustion** (sequence overflow): _(spin to next ms? error?)_ `____`

**Decision.** `__________`

**Why (vs alternatives).**
- vs **UUIDv4** — `__________` _(random → not sortable, index locality cost)_
- vs **DB sequence** — `__________` _(coordinated → SPOF / scaling bottleneck)_

---

## V2 — Cache stampede protection

**Context.** Redirects are the hot path (cache-aside over Redis). When a hot slug
expires, thousands of concurrent requests must not all rebuild from Postgres at
once. Implemented in `src/cache.rs::get_or_rebuild`.

**Options considered.**

| Strategy                          | How it prevents the stampede                          | Cost / downside                          |
|-----------------------------------|-------------------------------------------------------|------------------------------------------|
| Single-flight (in-process)        | One task rebuilds, others await the same future        | Per-node only; N nodes → N rebuilds      |
| Distributed lock (`SET NX PX`)    | One holder rebuilds cluster-wide; others wait/retry    | Lock TTL tuning; failure/fallback path   |
| Probabilistic early recompute     | Refresh *before* expiry with rising probability        | Tunable; occasional redundant rebuilds   |

**Decision.** `__________`

**Why.** `__________`
_(Note the failure mode you accepted: stale reads? a few duplicate rebuilds? a
brief wait? — and what happens if Redis is down.)_

**Negative caching.** Unknown slugs cached as `Missing` with a short TTL
(`MISSING_TTL_SECS`) so 404 floods don't reach Postgres. TTL chosen: `___` — why: `___`

---

## V3 — Async click ingestion / backpressure

**Context.** Recording analytics must never slow the redirect. Handler hands the
click off to a bounded channel + background batch-inserter and returns immediately.

**The bounded-channel question — what happens when analytics can't keep up?**

| Policy        | Behaviour at capacity                  | Trade you're making              |
|---------------|----------------------------------------|----------------------------------|
| Drop (shed)   | `try_send`; discard on full            | Lose some clicks, never slow redirect |
| Block         | `send().await`; redirect waits          | Exact counts, but backpressure → latency |
| Buffer/grow   | Larger queue / spill                    | Memory risk, delays the problem  |

**Decision.** `__________` _(channel capacity: `___`, batch size / flush interval: `___`)_

**Why.** `__________`
_(This is "trade exactness for throughput" — say which way you traded and why.)_

---

## Security — API-key comparison (constant-time?)

**Context.** `src/auth.rs::require_api_key` checks the bearer token against
`state.api_keys`. Today that's `HashSet::contains`, which is `O(1)` (constant-time
in *collection size*) but **not** guaranteed constant-time in the
*cryptographic* sense — the final `str ==` short-circuits on the first differing
byte, so its duration can leak how many leading bytes of a guess are correct (a
timing oracle that enables byte-by-byte key recovery).

> ⚠️ Two meanings of "constant time": `O(1)` (time vs. **how many** keys) ≠
> timing-safe (time vs. the **secret value**). The SPEC means the second.

**Options considered.**

| Option                              | How it works                                                        | Leaks timing? | Cost / complexity                          |
|-------------------------------------|---------------------------------------------------------------------|---------------|--------------------------------------------|
| **Keep `HashSet::contains`**        | Hash token (random-seeded SipHash) → bucket → short-circuiting `==`  | In principle yes, but buried under hashing noise + randomized layout | None — already done                        |
| **A. Constant-time loop (`subtle`)**| `ct_eq` token against **every** key, OR the results, **no early exit** | No            | `O(n)` per request; easy to get subtly wrong (an early `break` re-leaks) |
| **B. Compare hashes, not keys**     | Pre-hash keys → `HashSet<[u8;32]>`; hash incoming token, look that up | Only about the *digest*, which is non-invertible → useless to attacker | Low — `O(1)` kept; hash at `AppState` build + per request |

**Threat model (decide before choosing).**
- Keys are **in-memory**, behind randomized SipHash; exploiting the residual leak
  needs a statistical timing attack **over the network**, where jitter ≫ the signal.
- Is this service a target where that's worth defending? `yes / no — because ____`

**Decision.** `__________`
_(A valid outcome is "keep `HashSet::contains` because the residual risk is
acceptable for this threat model" — as long as that reasoning is written here.)_

**Why.** `__________`

**Related security notes.**
- **Keys hashed at rest:** _(SPEC asks for this — are keys stored hashed, or plain
  in memory/env? what would production do?)_ `__________`
- **Never logged:** ✅ token never reaches a log line in `require_api_key`.

---

## Open questions / deferred

- `__________`
