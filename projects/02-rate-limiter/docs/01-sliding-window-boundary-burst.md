# Sliding Windows & the Boundary Burst — From First Principles

> **What this teaches:** the exploit hiding in every fixed-window counter, and the
> two classic fixes — the exact-but-hungry *log* and the clever O(1) *weighted
> counter* — including exactly how approximate the counter is. No prior knowledge
> assumed beyond [doc 00](00-token-bucket.md).
>
> **Prepares you for:** [SPEC V2](../SPEC.md) — the `todo!()` in
> [sliding_window.rs](../src/sliding_window.rs).
> Concept card: [CONCEPTS.md · Card 2](../CONCEPTS.md).

---

## The one sentence to hold onto

> A fixed window resets on wall-clock boundaries an attacker can read, gifting
> them a 2× burst; a sliding window moves with `now` — and the weighted-counter
> trick buys that with just **two integers** instead of a timestamp per request.

---

## 1. The problem: the boundary burst

The simplest limiter anyone writes first: *"count requests this minute; reset the
counter at :00."* One counter, one increment, one comparison. It has a hole you
can drive double traffic through.

Limit: **100 per minute**. Watch an attacker who owns a clock:

```
          window [11:00:00 – 11:01:00)      window [11:01:00 – 11:02:00)
          count: 0 ────────────► 100        count: 0 ──► 100
  ────────┼──────────────────────────┼──────────────────────────┼────► time
          11:00:00            11:00:59│11:01:00
                                  ▲   │   ▲
                        100 requests  │   100 requests
                        (legal: count │   (legal: fresh window,
                         hits exactly │    count restarts at 0)
                         100)         │
                                      │
                        ◄─── 200 requests in ~2 seconds ───►
```

Each window is individually legal. But any observer measuring *any actual
60-second span* covering 11:00:59–11:01:59 sees up to **200 requests — 2× the
limit**. The reset instant is public knowledge (it's the wall clock), so this
isn't a fluke; it's a **repeatable exploit**: burst at :59, burst at :00, every
minute. If the limit exists to protect a database that falls over at 150/min,
fixed windows structurally cannot protect it. No tuning fixes this — the flaw *is*
the reset-on-a-boundary design.

Why doesn't the token bucket ([doc 00](00-token-bucket.md)) have this problem?
Because it has no boundaries at all — its state decays continuously. But a token
bucket makes a different promise ("burst X, sustain Y") than what some contracts
require: **"never more than N in any window of length W."** SLAs and quota
billing are often phrased exactly that second way, and that's the promise sliding
windows exist to keep. More on choosing between them in §5.

---

## 2. Fix #1 — the sliding window *log*: exact, and it bills you per request

The direct fix: stop counting *per boundary-aligned window* and count *the last W
seconds, from now*.

- **Store** the timestamp of every admitted request.
- **On each check:** evict timestamps older than `now − W`, count the survivors,
  admit if `count + cost ≤ limit`.

```
                     window of length W, ending at NOW (it slides!)
                  ┌──────────────────────────────────────┐
  ───●────●───●──┼──●───●──●───●────●──●───●──●──●───●───┼──► time
     ▲            └──────────────────────────────────────┘
     evicted                  count these: 11             NOW
     (older than now−W)
```

This is **exact** — it answers "how many in the last W seconds" with zero error,
because it keeps the raw data. And that's also the indictment: **memory is one
timestamp per admitted request per key.** At limit=10,000/min across many keys,
that's real memory, constantly churning.

The nastier property (Card 2's depth probe): the log's cost is proportional to
*traffic* — so it gets **most expensive exactly when you're under attack**, which
is precisely when the limiter must be cheapest. A component whose resource use
scales with attacker enthusiasm is a component the attacker controls.

When *is* the log acceptable? Low limits, few keys, or when exactness is a hard
requirement (billing disputes, strict SLAs). You must be able to state this
tradeoff in `docs/02-design.md` — it's a Done-when box.

---

## 3. Fix #2 — the sliding window *counter*: two numbers and a weighted guess

The clever version, and the one [sliding_window.rs](../src/sliding_window.rs)
scaffolds (this is Cloudflare's published design). Keep fixed windows — but keep
**two** of them, and *blend* across the boundary instead of resetting:

- `previous_count` — total of the last completed fixed window
- `current_count` — total so far in the current fixed window

To estimate "how many in the sliding window ending at `now`": the sliding window
covers **all** of the current fixed window so far, plus a **tail slice** of the
previous one. You don't know when the previous window's requests actually
arrived, so assume they were **spread uniformly** — then the fraction of them
still inside the sliding window is just the fraction of the previous window the
slice covers:

> estimate = previous_count × (overlap fraction) + current_count

A worked example — `W = 60 s`, `limit = 100`, and `now` is **15 s** into the
current window:

```
      previous fixed window          current fixed window
  ┌───────────────────────────┬───────────────────────────┐
  │        prev = 80          │ cur = 30                  │
  └───────────────────────────┴───────────────────────────┘
           ▲                                ▲
           │◄───── sliding window W ───────►│
           │      (60s ending at now)      now = +15s
           │
   covers the last 45s of prev  →  overlap fraction = 45/60 = 0.75
```

| Quantity | Value |
|---|---|
| `previous_count` | 80 |
| `current_count` | 30 |
| Overlap fraction | (60 − 15) / 60 = **0.75** |
| Estimate | 80 × 0.75 + 30 = **90** |
| Decision for a cost-1 request | 90 + 1 ≤ 100 → **allow** |

(Arithmetic verified.) Total state per key: **two counters and one window-start
timestamp** — O(1), independent of traffic volume. The attack that bloats the log
does nothing to the counter's memory.

The boundary burst is closed: right after a boundary, the previous window's 100
requests still weigh in at fraction ≈ 1.0, so the estimate is ≈ 100 and the
second burst is denied — the exact moment the fixed counter would have said
"fresh window, come on in."

---

## 4. How wrong can the counter be? (Be honest about this)

The uniform-arrival assumption is the entire approximation. When is it wrong, and
by how much? Card 2's depth probe asks: *an attacker knows you use the weighted
counter — can they still squeeze out more than the limit?*

Yes — here's the worst case, worked. The attacker sends all `limit` requests **at
the very end of the previous window** (clustered at the boundary), then keeps
pushing. At a fraction `f` into the current window:

- **Truth:** all `limit` previous requests are still inside the real sliding
  window (they're only `f·W` old), so the true count is `limit + current`.
- **Estimate:** `limit × (1 − f) + current` — the uniform assumption *ages them
  out linearly* even though none have actually left.

So the counter under-counts by `limit × f`, and the attacker can push `current`
up to `limit × f` extra admitted requests. Verified for `limit=100`:

| Fraction `f` into current window | True count in a real W-window can reach |
|---|---|
| 0.5 | 150 |
| 0.9 | 190 |
| 0.99 | 199 |

**In the adversarial worst case, the weighted counter degrades toward the same 2×
the fixed window gives up** — but with crucial differences: it takes deliberate
clustering (not just reading a clock — the attacker must *also* be denied nothing
during the setup burst), it can't be repeated back-to-back (the clustered window
becomes `previous` and weighs against them), and under anything like uniform
traffic the estimate is tight. Cloudflare measured ~0.003% of requests
wrongly actioned in production. The SPEC's Done-when asks you to *document an
error bound* — this analysis is the shape of it; your `docs/02-design.md` should
state the bound your implementation and tests actually demonstrate.

---

## 5. Token bucket vs sliding window — when each

| | **Token bucket** (V1) | **Sliding window counter** (V2) |
|---|---|---|
| The promise | "Burst up to C, sustain R/s" | "Never more than N in any W-length span" (approx.) |
| Bursts | First-class, by design | Tolerated up to N, then hard stop |
| State per key | 2 numbers (tokens, last_refill) | 3 numbers (2 counts, window start) |
| Boundary artifact | None (no boundaries) | Closed (that's its job) |
| Natural fit | Client-friendliness: APIs where bursts are normal | Contract enforcement: SLAs, quotas, "per minute" pricing tiers |

This project makes you build both and wire either behind the same gRPC surface —
[limiter.rs](../src/limiter.rs)'s `Algorithm` enum is selected by the `ALGORITHM`
env var in [main.rs](../src/main.rs). Same `LimitConfig` in, same `Decision` out.

One scaffold detail worth noticing:
[`SlidingWindowCounter::new`](../src/sliding_window.rs) *derives* the window from
the shared config — `window = burst / rate_per_sec`, `limit = burst` (with the
defaults `burst=20, rate=10/s`: a 2-second window of 20). That's the bridge
between the two vocabularies: "20 per 2s" is the sliding-window reading of "burst
20 at 10/s." They are *not* equivalent contracts (see the table), which is
exactly why the SPEC has you compare their boundary behavior.

---

## 6. The design space — what the `todo!()` leaves to you

The scaffold's TODO sketches three steps (roll windows forward → estimate →
admit-or-deny). The decisions that remain are the learning:

1. **Window rollover.** `now` may land in the next fixed window — or *several*
   windows later (an idle key). What must `previous_count` become in each case?
   Getting the skipped-a-whole-window case wrong silently haunts idle keys (the
   scaffold's test TODO calls this one out).
2. **The deny's `retry_after`.** For the bucket it was `deficit / rate`. Here,
   budget frees up as the previous window's weight *rolls off*. What's a truthful
   wait derived from that?
3. **Cost accounting.** Do denied requests count toward the window? (Think: what
   does each choice do to a client that's hammering while limited?)
4. **Your error-bound statement.** What bound will your boundary-burst test
   actually assert, and how will you phrase it in `docs/02-design.md`?

The headline test is spelled out in the scaffold's test TODO: **show the fixed
window admitting ~2× across a boundary, and your sliding counter refusing it.**
Stuck? `/hint` for graduated nudges, `/quest` for the guided build.

---

## 7. Mental model summary

| Concept | One-liner |
|---|---|
| Boundary burst | Fixed windows reset at instants attackers can read → repeatable 2× every boundary |
| Sliding window | The window ends at `now`, always — no boundary to camp on |
| Log | Exact; costs a timestamp per request; grows *with the attack* |
| Counter | Two counts + a weighted blend; O(1); assumes uniform arrivals |
| The weighting | `prev × overlap_fraction + current` — aging out the previous window linearly |
| Error bound | Tight for smooth traffic; adversarial clustering can approach 2× once, not repeatably |
| vs token bucket | Different *promises*: burst-friendliness vs "never >N in any W" |

## Where you'll build this

**Module:** [src/sliding_window.rs](../src/sliding_window.rs) — the
`todo!("V2: sliding-window-counter decision")` in `try_acquire`, plus the
boundary-burst test the module's test TODO describes.

**Done-when criteria this unlocks** ([SPEC V2](../SPEC.md)):

- [ ] Boundary burst demonstrated against a fixed counter — and absent here
- [ ] Counter implemented with O(1) memory (current + weighted previous)
- [ ] Decisions within a documented error bound of the exact log
- [ ] Log-vs-counter tradeoff stated in `docs/02-design.md`

V3 will ask you to make *one* of these algorithms atomic across N instances —
carry the counter's simplicity in mind when you get there.
