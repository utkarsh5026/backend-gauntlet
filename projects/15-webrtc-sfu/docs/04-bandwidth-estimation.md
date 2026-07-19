# Bandwidth Estimation — Measuring a Link Nobody Describes to You

> How the SFU figures out, per subscriber, how many bits per second their
> downlink can take *right now* — from nothing but feedback about packets it
> already sent. No prior congestion-control knowledge assumed; the queue
> physics is derived from scratch.
>
> Prepares you for **V4** in [SPEC.md](../SPEC.md) — the
> [`BandwidthEstimator`](../src/bwe.rs) (`on_transport_feedback`, `on_loss`)
> and [`Allocator`](../src/bwe.rs) (`split`) — all `todo!()`. Its output is
> exactly what V3's [`LayerSelector::set_budget`](../src/simulcast.rs)
> consumes.

---

## 0. The one sentence to hold onto

**Nobody tells the SFU a subscriber's downlink capacity — it must be
estimated from feedback, per subscriber, using two signals: the delay
gradient (a queue *building* — early, subtle) and the loss fraction (a queue
*overflowed* — late, certain), blended conservatively and clamped, closing
the loop back into layer selection.**

---

## 1. The problem: sending above capacity hurts more than this call

Somewhere between the SFU and each viewer there's a bottleneck — the cell
tower, the DSL line, the hotel wifi. Model it as a pipe with a **queue** in
front:

```
   SFU sends at R bps ──▶ [ queue ] ──▶ bottleneck drains at C bps ──▶ viewer

   R < C : queue stays empty; packets arrive as smoothly as they were sent
   R > C : queue GROWS by (R − C) — every queued byte is added latency
           …until the queue is full, and then packets are DROPPED
```

Two consequences make this the SFU's problem, not the network's:

1. **The failure is graded, and the good signal comes first.** Before any
   packet is lost, latency climbs (the queue filling — "bufferbloat"). Loss
   is the *late* signal; delay is the *early* one. A controller that waits
   for loss has already ruined the experience — in a live call, the latency
   *is* the product.
2. **Each viewer's bottleneck is their own.** The trap in
   [CONCEPTS.md](../CONCEPTS.md): one global estimate ("the server's uplink
   is shared, average it") starves fibre viewers and drowns mobile ones
   *simultaneously*, because the constraint is each viewer's private
   downlink. Hence: one [`BandwidthEstimator`](../src/bwe.rs) **per
   subscriber**, each running its own loop. (This is project 14's congestion
   controller, moved server-side and multiplied by N viewers.)

And the capacity *moves* — someone starts a download, the train enters a
tunnel, the wifi clears. The estimate must track it: converge, back off,
recover.

## 2. Signal one: the delay gradient (early, subtle)

The SFU knows when it **sent** each packet. Via transport-wide feedback
(TWCC — the subscriber periodically reports "packet #N arrived at time T"),
it learns when each **arrived**. Absolute clocks don't align across machines
— but you never need them to. Compare *spacings*:

> If packets **sent 10 ms apart** start **arriving 15 ms apart**, the extra
> 5 ms per packet is time spent *sitting in a growing queue*. The link is
> telling you it can't drain what you're sending — **before dropping
> anything.**

A worked batch (the shape of [`ArrivalSample`](../src/bwe.rs) — `sent_ms`,
`arrived_ms`; the constant receiver-clock offset cancels in the differences):

| Packet | Sent (ms) | Arrived (ms) | Send gap | Arrival gap | Gradient |
|---:|---:|---:|---:|---:|---:|
| 0 | 0  | 50  | —  | —  | — |
| 1 | 10 | 62  | 10 | 12 | **+2 ms** |
| 2 | 20 | 75  | 10 | 13 | **+3 ms** |
| 3 | 30 | 89  | 10 | 14 | **+4 ms** |
| 4 | 40 | 104 | 10 | 15 | **+5 ms** |

Consistently positive and *rising*: a queue is building — **over-use**; ease
off now. Gradients hovering around zero: the path is clear — room to probe
up. Negative: a queue *draining* (you already backed off; it's emptying).

The catch, and why this signal needs care: single gradients are **noisy**
(OS scheduling jitter, wifi retransmits, bursty cross-traffic all fake ±ms).
The raw per-packet number is never used directly — some smoothing/trend
detection over the batch separates "queue building" from "one jittery
packet". How much smoothing is a core design choice (§5).

## 3. Signal two: the loss fraction (late, certain)

RTCP receiver reports carry `fraction_lost` — what share of packets never
arrived. By the time loss is sustained, the queue already overflowed: you're
not near the limit, you're **past** it. Blunt, laggy — but unambiguous, and
immune to the delay signal's noise. The scaffold's
[`on_loss`](../src/bwe.rs) TODO states the classic GCC rule of thumb it
wants:

| `fraction_lost` | Regime | Action |
|---|---|---|
| ≳ 10% | Overloaded | Multiplicative decrease — `estimate *= 1 − 0.5·loss` (e.g. 2 Mbps at 20% loss → **1.8 Mbps**, and again next report, and again — compounding until loss stops) |
| 2%–10% | Ambiguous | Hold |
| ≲ 2% | Clean | Additive increase — probe upward |

That's **AIMD** — additive increase, multiplicative decrease — the same
asymmetry TCP uses, and for the same reason: probe gently (the cost of being
slightly under is mild), retreat hard (the cost of staying over compounds).

### Blending: the lower estimate wins

Each signal has a failure mode — delay: noise (false alarms); loss: latency
(true alarms, too late). The combination rule is conservative: **min** of
the two. If *either* says trouble, believe it; claiming more room than the
gloomier signal supports is how queues get built. And always **clamp to
`[min_bps, max_bps]`** ([`new`](../src/bwe.rs) already clamps the start):
the V4 criterion says *no sequence of hostile or garbage feedback* — absurd
timestamps, NaN-inducing spacings, loss > 100% — may drive the estimate
negative, zero-stuck, unbounded, or NaN. On an open port, feedback is
attacker input like everything else.

## 4. The closed loop — and what stability means for a human

The estimate isn't a report; it's an **actuator**. The full loop, per
subscriber:

```
        feedback (TWCC batches, RR loss)
              │
              ▼
   BandwidthEstimator ──estimate──▶ Allocator::split ──per-stream budget──▶
   LayerSelector::set_budget ──layer choice──▶ changed send rate ──▶
   the LINK responds (queue grows/drains) ──▶ new feedback ──▶ …
```

Closing the loop is what makes convergence and oscillation *product*
qualities: an estimator that oscillates around a layer boundary is a viewer
whose video pops between resolutions every few seconds. The V4 criteria
encode the control-theory contract directly: on a link capped at C,
**settle near C** (within a documented margin) without wild oscillation;
after a capacity drop, **back off within bounded time**; once the link
clears, **climb back**. The boss fight then measures it end-to-end: capped
subscribers converge to the low layer and fibre subscribers to high within
≤ 3 s of joining, through a mid-call sag and recovery.

### The allocator: why you never hand out 100%

[`Allocator::split`](../src/bwe.rs) divides a subscriber's budget across the
streams they receive (trivial for one video stream; real for camera +
screen-share). One rule is non-negotiable: **reserve headroom — never
allocate the full budget.** Two derivations for the same rule:

- *Measurement:* you can only discover the link improved by occasionally
  sending a little more than "exactly enough". Saturate the estimate
  precisely and the feedback can never show slack — the estimate can never
  climb. Headroom is where probing lives.
- *Safety:* the estimate is approximate and the link is moving; allocating
  100% of an approximation means half your errors are over-commitments.

There's also a granularity mismatch to sit with (a
[CONCEPTS.md](../CONCEPTS.md) depth probe): layers come in steps
(150/500/2000 kbps) but the estimate is continuous — a 600 kbps estimate
selects mid at 500 and *can't use* the remaining ~100. Priorities across
streams (camera over screen-share? equal?) are yours to choose and document.

## 5. The design space (what's yours to decide)

The scaffold deliberately leaves the control law open — "even a clean
loss-based AIMD with a delay-gradient trigger passes — but it must converge,
back off, and recover." Your decisions for `docs/15-design.md`:

- **Over-use detection:** how a batch of noisy gradients becomes a
  trustworthy building/clear/draining verdict — smoothing, a trend over the
  batch, a threshold? (Real GCC fits a trendline and adapts its threshold;
  how far down that road to go is a scope call.)
- **The constants:** additive step, multiplicative factor, the hold band.
  These trade convergence speed against oscillation — the capacity-step
  simulation in the bench harness is where you'll *see* the tradeoff.
- **Blending mechanics:** the min-rule is stated; what state each side
  keeps, and when each updates (TWCC batches vs RR cadence), is structure
  you own. Keep it O(1) per sample — the scaffold notes this runs on the
  per-subscriber feedback hot path.
- **Headroom + split policy** for the allocator.

`/hint` for nudges, `/quest` to build against the acceptance tests.

## 6. Mental model summary

| Concept | The rule | Why |
|---|---|---|
| Per-subscriber estimator | One loop per viewer, server-side | Each downlink is a private, unshared bottleneck |
| Delay gradient | Arrival spacing − send spacing, smoothed | Sees the queue *building* — pre-loss, but noisy |
| Loss fraction | ≳10% cut hard, ≲2% probe, between hold | Certain but late — the queue already overflowed |
| Blend | The lower estimate wins | Either signal saying trouble is trouble |
| Clamp | Always within `[min, max]`, never NaN | Feedback is attacker input on an open port |
| AIMD | Probe gently, retreat hard | The costs of under vs over are asymmetric |
| Allocator | Sum ≤ budget, headroom always reserved | Probing needs somewhere to live; estimates are approximate |
| The loop | measure → estimate → select → re-measure | Oscillation here is visible quality flapping |

## 7. Where you'll build this

[`BandwidthEstimator::on_transport_feedback`](../src/bwe.rs),
[`BandwidthEstimator::on_loss`](../src/bwe.rs) and
[`Allocator::split`](../src/bwe.rs) in [bwe.rs](../src/bwe.rs). The wired
core feeds RTCP into them from [`Sfu::handle_rtcp`](../src/sfu.rs) and pipes
the estimate into V3.

This doc unlocks V4's **Done when ALL true** ([SPEC.md](../SPEC.md)):
delay-signal reaction, loss-signal reaction with conservative blending,
clamped robustness, convergence + recovery on capacity steps, and allocator
headroom — proven by `backs_off_on_rising_delay`, `backs_off_on_loss`,
`recovers_on_clear`, `stays_clamped`, `allocator_reserves_headroom`, plus
the capacity-step simulation in the bench harness.
