# The Token Bucket — Rate vs Burst, From First Principles

> **What this teaches:** what "allow N requests per second" actually means, why the
> naive readings of it all fail, and the two-knob model (burst + sustained rate)
> that the token bucket algorithm encodes. No prior rate-limiting knowledge assumed.
>
> **Prepares you for:** [SPEC V1](../SPEC.md) — the `todo!()` in
> [token_bucket.rs](../src/token_bucket.rs), speaking the shared vocabulary of
> [limiter.rs](../src/limiter.rs) (`LimitConfig` in, `Decision` out).
> Concept card: [CONCEPTS.md · Card 1](../CONCEPTS.md).

---

## The one sentence to hold onto

> A token bucket separates **"how much can you send at once"** (capacity) from
> **"how fast can you keep sending"** (refill rate) into two *independent* knobs —
> and computes refill **lazily from elapsed time**, so a million idle buckets cost
> exactly nothing.

---

## 1. The problem: "100 requests per second" is ambiguous

Say your API's terms are "100 requests per second." A client opens your dashboard
page, and the page fires **15 API calls in 50 ms** — a completely normal page load.
Is that legal?

It depends what "100/s" means, and every naive reading breaks somewhere:

| Naive interpretation | How it works | How it breaks |
|---|---|---|
| **Smooth spacing** — one request allowed every 10 ms | Timer math: `now - last_request >= 1/rate` | The 15-call page load gets 1 allowed, 14 denied. You just punished *normal* traffic. Real clients are bursty. |
| **Fixed quota** — count requests, reset the counter every second | `count += 1; deny if count > 100` | A client can send 100 at 11:00:00.999 and 100 more at 11:00:01.000 — **200 requests in 2 ms**, all "legal." (This boundary hole is the entire subject of [doc 01](01-sliding-window-boundary-burst.md).) |
| **Total budget** — 8.64M requests per day, spend anytime | One big counter | An abuser front-loads the whole day's budget in a minute and flattens you; a well-behaved client gets nothing special. |

The tension: **bursts are normal, sustained floods are abuse** — and one number
can't distinguish them. You need two.

---

## 2. The idea: a bucket of tokens

Picture a bucket that holds coins:

- The bucket holds at most **`capacity`** tokens — this is the **burst** allowance.
- Tokens drip in at **`rate`** tokens per second — the **sustained** allowance.
- When the bucket is full, new tokens **overflow and are lost** (you can't hoard
  a week of idle time into an infinite burst).
- Every request must **spend** tokens (usually 1, but `Check(key, cost)` lets a
  heavy operation cost more). No tokens → denied.

```
            refill: rate tokens/sec
                     │ │ │
                     ▼ ▼ ▼
              ┌───────────────┐  ─── overflow above `capacity`
              │  ● ● ● ● ●    │       is discarded
              │  ● ● ●        │
              └───────┬───────┘
                      │ spend `cost` tokens
                      ▼
              request admitted   (bucket empty → denied)
```

Now the two client behaviors separate cleanly:

- The **page load** (15 calls at once): fine — the bucket held ≥15 tokens, the
  burst drains them, done.
- The **sustained flood**: the bucket empties, and from then on the client is
  paced at exactly `rate` — no matter how hard they hammer.

**The two knobs are genuinely independent.** `capacity=20, rate=10/s` (this
project's defaults, see [main.rs](../src/main.rs) reading `BURST` and
`RATE_PER_SEC`) means: "you may fire 20 at once, but you'll average 10/s over any
long stretch." `capacity=1000, rate=1/s` is a totally different contract: huge
rare bursts, glacial sustained rate. One number could never express either.

---

## 3. The defining trick: refill *lazily*, on read

The obvious implementation has a background task ticking tokens into the bucket:

```
every 100ms:
    for each bucket:            // ← the problem is this line
        bucket.tokens += rate * 0.1
```

Why is this a trap? This limiter keys on API key / user id / client IP
(`Check(key, cost)` — see [ratelimit.proto](../proto/ratelimit.proto)). The number
of keys is **unbounded and mostly idle**. With 1,000,000 known keys and a 100 ms
tick you're doing **10,000,000 bucket updates per second** — almost all of them
for keys that will never send another request. The timer does work proportional
to *how many clients exist*, not *how many are talking*.

The fix inverts the direction of work: **nobody touches a bucket until that
bucket's key makes a request.** On each check, you reconstruct what the bucket
*would* contain from the elapsed time:

> tokens accrued since last look = `elapsed_seconds × rate`, capped at `capacity`

That single line of math replaces the timer entirely. Between requests, a bucket
is just **two numbers at rest** — `tokens` and `last_refill` — costing zero CPU.
A worked trace with `capacity=20, rate=10/s`:

| Wall time | Event | Elapsed since last refill | Tokens before spend | Spend | Tokens after |
|---|---|---|---|---|---|
| t=0.0s | bucket created (starts **full**) | — | 20.0 | — | 20.0 |
| t=0.0s | burst: 15 requests, cost 1 each | 0s → +0 | 20.0 | 15 | 5.0 |
| t=0.3s | 1 request | 0.3s → +3.0 | 8.0 | 1 | 7.0 |
| t=10.0s | 1 request | 9.7s → +97.0, **capped at 20** | 20.0 | 1 | 19.0 |
| t=10.0s | 25 more requests instantly | 0s → +0 | 19.0 | 19 allowed, **6 denied** | 0.0 |

Two things to notice in that table:

- **The cap does real work.** At t=10.0s the client had "earned" 97 tokens of
  idle time but only 20 fit — idleness never converts into a mega-burst. (Card 1's
  depth probe: idle an hour at `rate=10/s`? You accrued 36,000 tokens' worth of
  time; capacity still says 20. Verified: 3600 × 10 = 36,000.)
- **A fresh bucket starts full** — the scaffold's
  [`TokenBucket::new`](../src/token_bucket.rs) already encodes this. First
  impression of your API shouldn't be a denial.

Note the scaffold's signature: `try_acquire(&mut self, cost: u64, now: Instant)`.
Time is **injected**, not read inside — that's what lets your tests drive the
clock deterministically instead of `sleep()`ing.

---

## 4. A truthful `retry_after`

On a deny, the response carries `retry_after` (see `Decision::deny` in
[limiter.rs](../src/limiter.rs), and `retry_after_ms` on the wire). This must be
**truthful**: the exact time until enough tokens will exist.

The reasoning: you know the **deficit** (`cost - tokens`) and you know the refill
**rate**, so the wait is `deficit / rate`. Concretely: bucket holds 2.5 tokens,
request costs 5, rate is 10/s → deficit 2.5 tokens → **0.25 s** (verified:
(5 − 2.5) / 10 = 0.25).

Why does truthfulness matter enough to be a Done-when box? Because clients *act*
on this number. Report too short, and every denied client retries early, gets
denied again, and retries again — you've converted one over-limit request into a
polling loop hammering your hot path. Report too long, and you silently degrade
well-behaved clients. The SPEC's criterion is checkable end to end: *wait exactly
`retry_after`, and the next check must succeed.*

---

## 5. The precision trap

The scaffold stores `tokens: f64` with the comment *"Fractional on purpose —
don't round away budget."* Here's the trap it's warning about.

Suppose you kept tokens as an integer and rounded on every refill. At
`rate=10/s`, a request arriving every 30 ms accrues 0.3 tokens per refill —
which rounds **down to 0** every single time. The client is entitled to 10
tokens/s and receives **zero, forever**. Round *up* instead and you mint free
budget on every call. Either way, per-call rounding compounds: the error is
per-refill, and refills happen millions of times.

Floating point has its own, much smaller, version of this. Binary floats can't
represent most decimals exactly — verified in this repo's environment:

```
0.1 + 0.2               = 0.30000000000000004
0.1 added 3600 times    = 360.00000000001336   (error ≈ 1.3e-11)
```

An `f64` accumulates *nanotoken*-scale error — harmless in practice, and the SPEC
accepts it — but the *pattern* to internalize is: **the fewer times you round, and
the later you round, the less budget you mint or destroy.** How you represent
tokens (fractional? integer micro-tokens? recompute-from-timestamps?) is one of
the design decisions below. The Done-when box — *"neither manufactures nor loses
budget across many refills (property-tested)"* — is exactly a test for this: hit
the bucket thousands of times at awkward intervals and assert the long-run
admitted rate equals the configured rate.

---

## 6. Token bucket vs leaky bucket — not the same algorithm

You'll see these conflated constantly. They answer different questions:

| | **Token bucket** | **Leaky bucket** |
|---|---|---|
| Mental picture | Tokens drip **in**; requests spend them | Requests queue in the bucket; they leak **out** at a fixed rate |
| Bursts | **Admitted** instantly, up to capacity | **Smoothed** — output is always the steady leak rate |
| What it shapes | *Admission* (yes/no now) | *Output timing* (when each item proceeds) |
| Typical home | API rate limiting (this project) | Traffic shaping — nginx `limit_req`, network QoS |

If a page load fires 15 calls: token bucket admits all 15 *now*; leaky bucket
trickles them out one per `1/rate`. For an API limiter you almost always want
token-bucket semantics — clients feel latency, and bursts are legitimate.

**In the wild:** AWS documents its API throttling as literal token buckets with
published capacities; Stripe's limiter is token-bucket-based; Linux `tc` uses
token buckets for shaping.

---

## 7. The design space — decisions the `todo!()` leaves to you

This doc stops at the door of [`try_acquire`](../src/token_bucket.rs). The
scaffold's TODO comments already sketch the three steps (refill → spend-or-deny →
truthful retry). What's left — the interesting part — is the decisions:

1. **Order of operations.** Refill and spend must see one consistent `now`. What
   happens if you deduct before refilling, or refill twice?
2. **The cap and the clock.** When exactly do you clamp to capacity, and when do
   you advance `last_refill`? (Advancing it without crediting the elapsed tokens
   *destroys* budget — one of the drift bugs the property test should catch.)
3. **`retry_after` edge cases.** What if `cost > capacity` — the request can
   *never* succeed? What's truthful then?
4. **Representation.** Stay `f64` as scaffolded, or argue for something else in
   your design doc? What does your property test bound?

When you're stuck, `/hint` gives graduated nudges; `/quest` runs the guided build
with acceptance tests written before you implement.

---

## 8. Mental model summary

| Concept | One-liner |
|---|---|
| Capacity (burst) | The most you can do *right now* — the bucket's size |
| Rate (sustained) | Your long-run average — the drip rate |
| Independence | `20 burst @ 10/s` and `1000 burst @ 1/s` are different contracts one number can't express |
| Lazy refill | `min(capacity, tokens + elapsed × rate)` on read — work scales with *traffic*, not *number of keys* |
| Overflow | Idle time never converts to unlimited burst — the cap discards excess |
| Truthful `retry_after` | `deficit / rate`; lying trains clients to hammer you |
| Precision | Round rarely and late; per-call rounding compounds into minted/destroyed budget |
| Leaky bucket | The *other* algorithm: smooths output instead of admitting bursts |

## Where you'll build this

**Module:** [src/token_bucket.rs](../src/token_bucket.rs) — the
`todo!("V1: lazy token-bucket refill + acquire")` in `try_acquire`, plus the test
module below it (burst-then-throttle, cap, truthful retry, zero drift).

**Done-when criteria this unlocks** ([SPEC V1](../SPEC.md)):

- [ ] Capacity and refill rate are two independent knobs
- [ ] Refill is lazy — no background timer or per-bucket task
- [ ] Burst admitted at once; sustained traffic settles to the rate
- [ ] Denied checks return a truthful `retry_after`
- [ ] No rounding drift across many refills (property-tested)

Get V1 airtight in-process — V3 is *the same math, made atomic inside Redis*.
