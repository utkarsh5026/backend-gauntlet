# Simulcast — Quality Per Viewer Without Decoding a Pixel

> How one publisher serves a fibre viewer and a 3G viewer *different quality*
> while the server never touches a codec. No prior video-codec knowledge
> assumed — the keyframe/delta-frame model is built up from scratch, because
> it's the constraint the whole vertical hangs on.
>
> Prepares you for **V3** in [SPEC.md](../SPEC.md) — the
> [`LayerSelector`](../src/simulcast.rs) (`set_budget`, `wants_keyframe`,
> `on_packet`, `selected_bitrate` — all `todo!()`), composing with V2's
> [`Rewriter`](../src/forward.rs).

---

## 0. The one sentence to hold onto

**The publisher encodes the same video several times at different qualities
and uploads them all; the SFU forwards exactly one per subscriber — the
highest that fits their budget — and may only switch *up* at a keyframe,
which is why the selector's real job is managing *when* a switch is allowed
to land.**

---

## 1. The problem: one encoding cannot serve unequal links

Say the publisher sends a single 2 Mbps encoding. Per subscriber:

| Subscriber's downlink | Result of forwarding 2 Mbps |
|---|---|
| Fibre, 100 Mbps | Fine. |
| Throttled mobile, 600 kbps | The link's queue fills, latency balloons, then sustained loss — the call *drowns*. |

Send 300 kbps instead and the mobile viewer is fine while the fibre viewer
watches pixel soup. There is no single right rate — the *right rate is a
property of each viewer*, and an SFU forbids itself the MCU's answer
(re-encode per viewer — doc [00](00-why-an-sfu.md)). If adaptation can't
happen by re-encoding, it must happen by **choosing among encodings** — which
means the publisher must offer more than one.

## 2. Simulcast: the publisher pays so the server doesn't

**Simulcast** = the publisher encodes the same picture ~3 times and sends all
of them, each as its own RTP stream (own SSRC, labeled with an RID). This
project's canonical layers (see the [SPEC's](../SPEC.md) publish example and
[`SimulcastLayer`](../src/simulcast.rs)):

| RID | SSRC (example) | Bitrate | Role |
|---|---|---:|---|
| `q` (quarter) | 111 | ~150 kbps | The floor — everyone can take this |
| `h` (half) | 222 | ~500 kbps | The middle |
| `f` (full) | 333 | ~2 Mbps | The ceiling for good links |

The cost lands on the **publisher**: encoding 3× (CPU) and uploading
150 + 500 + 2000 = **2.65 Mbps ≈ 1.33×** the 2 Mbps top layer alone. That's
the deal: a modest, constant publisher tax buys the server per-viewer
adaptation with zero transcoding — and it's why mobile publishers sometimes
send only two layers, or one (the depth probe in
[CONCEPTS.md](../CONCEPTS.md)).

The design space has three points, and you should be able to place them on
the cost/flexibility curve cold:

| Approach | Publisher cost | Server cost | Adaptation granularity |
|---|---|---|---|
| **Transcode (MCU)** | 1 encode, 1 upload | per-pixel CPU per viewer | perfect, any rate |
| **Simulcast** | ~3 encodes, ~1.3× upload | per-packet only | coarse — pick 1 of ~3 |
| **SVC** (scalable video coding: one encoding with peelable layers) | 1 fancier encode | per-packet only | finer (drop layers) — but needs codec support end-to-end |

Simulcast won the mainstream because it works with plain encoders/decoders —
`sendEncodings` in every browser's WebRTC API *is* simulcast.

## 3. The constraint everything hangs on: keyframes

A video encoder does not compress each frame independently — that would waste
enormous bandwidth on redundancy between near-identical frames. Instead:

- A **keyframe** (I-frame) is a complete, self-contained picture. Expensive
  (often 5–10× a delta frame's bytes), sent rarely.
- Every other frame is a **delta**: "take the previous picture and change
  these blocks". Cheap — and *meaningless without its references*.

```
   layer f (high):   K ─ d ─ d ─ d ─ d ─ d ─ d ─ K ─ d ─ d ─ …
                     ▲ complete    each d references what came before

   A decoder that tunes in at a `d` has nothing to apply the delta TO.
   It can only start cleanly at a K.
```

Now the switching rule falls out by derivation, not decree:

- **Switching up** (say `h` → `f`): the subscriber has never received *any*
  `f` packet. Every `f` delta references `f` frames it doesn't have. Feed it
  deltas anyway and the decoder produces smeared garbage that self-heals at
  the next keyframe — the maddening intermittent corruption named as the trap
  in [CONCEPTS.md](../CONCEPTS.md). So an up-switch must **wait for a
  keyframe on the target layer**.
- Keyframes are rare, so you don't just wait — you *ask*: send a *keyframe
  request* upstream to the publisher (**PLI**, Picture Loss Indication — see
  doc [05](05-the-wire-and-the-guardrails.md) for PLI vs FIR) so the target
  layer produces a fresh K soon.
- **Switching down** (`f` → `h`): this project's model treats it as
  **immediate** — the SPEC's criterion is "a budget drop switches without
  waiting". The asymmetry to internalize is *urgency*: an up-switch is an
  optimization (you can afford to keep sending the working layer while you
  wait), a down-switch is damage control (the budget says you're *already*
  over — continuing to send the expensive layer while politely waiting
  defeats the point). Honestly noted: a real decoder still prefers a clean
  boundary on any switch, and production SFUs pair the immediate down-switch
  with a PLI so any brief artifact heals at the next keyframe; the SPEC
  simplifies to "down = now" to keep the state machine honest about the part
  that matters.

### The up-switch, step by step

Budget rises from 600 kbps to 3 Mbps while forwarding `h`:

```
 1. set_budget(3_000_000)   target := f (highest fitting) — but f > h: up-switch
                            keyframe now OWED; keep forwarding h
 2. wants_keyframe() → true core sends ONE PLI upstream to the publisher
 3. packets keep arriving:  h-packets → Forward (still the safe layer)
                            f-delta   → Drop (can't start here)
                            q-packet  → Drop (never selected)
 4. f-KEYFRAME arrives      COMMIT: current := f, keyframe-owed clears,
                            this very packet → Forward
 5. from here               f → Forward, h → Drop, q → Drop
```

Two disciplines hide in there:

- **Exactly one PLI per pending switch.** `wants_keyframe` exists so the
  wired core requests once, not once per packet — a **PLI storm** forces the
  publisher to emit keyframe after keyframe (each 5–10× normal size),
  inflating everyone's bitrate: the cure becomes the congestion.
- **Never forward nothing.** A budget below even the lowest layer still
  selects the lowest — a starving viewer at least gets *something* to decode,
  and the estimator (V4) needs flowing packets to ever measure recovery.

## 4. Composition with V2: why the switch is invisible

The selector never talks to the subscriber — it decides, per origin packet,
[`Decision::Forward` or `Decision::Drop`](../src/simulcast.rs). Then:

```
   origin packet (ssrc, is_keyframe)
        │
        ▼
   LayerSelector::on_packet ──── Drop ───▶ Rewriter::skip     (outbound line stays gapless)
        │
      Forward
        ▼
   Rewriter::rewrite  ──▶ stable ssrc, next seq, smooth ts ──▶ subscriber
```

A layer switch is nothing more than *which origin SSRC gets `Forward`*
changing — the rewriter (doc [02](02-per-subscriber-rtp-rewriting.md))
absorbs the SSRC/seq/ts discontinuity by construction. That's the "switch is
invisible" criterion: V2 composed with V3, exercised together.

## 5. The design space (what's yours to decide)

- **The selection rule's margin.** "Highest layer whose bitrate ≤ budget" is
  the floor of the policy. But an estimate hovering at 510 kbps around a
  500 kbps mid layer will flap mid↔low on every estimator tick — each flap a
  PLI and a visible quality pop. What damping do you add — hysteresis bands?
  a margin below budget? switch-rate limiting? The SPEC explicitly asks the
  design doc to answer ("hysteresis to avoid flapping? margin below
  budget?").
- **State for the pending switch.** Current layer, pending target,
  keyframe-owed — the scaffold names the *categories*; the transitions (what
  happens if the budget drops *while* an up-switch is pending? if the target
  rises again before the keyframe lands?) are the part worth thinking
  through before coding.
- **PLI vs FIR** as the request mechanism, named in `docs/15-design.md`.
- **How keyframes are recognized** at the forwarding layer (the wired core
  passes `is_keyframe` into `on_packet`; the [`RtpView`](../src/wire.rs)
  marker bit flags frame *ends* — worth noticing which question each
  answers).

That's the door — `/hint` for nudges, `/quest` to build it against
acceptance tests.

## 6. Mental model summary

| Concept | The rule | Why |
|---|---|---|
| Simulcast | Publisher sends ~3 encodings, each its own SSRC/RID | Adaptation by *selection* needs options to select among |
| Selection | Highest layer with bitrate ≤ budget; never nothing | Budget is a ceiling; a starving viewer still needs packets |
| Up-switch | Owe a keyframe → one PLI → commit at target's K | Deltas are meaningless without their references |
| Down-switch | Immediate (this project's model) | Over budget *now*; waiting defeats the purpose |
| PLI discipline | One per pending switch | PLI storms inflate everyone's bitrate |
| Invisibility | Switch = change which origin feeds the rewriter | V2 owns the downstream identity |
| Flapping | Damping policy — yours to design + document | Oscillating estimates otherwise become oscillating quality |

## 7. Where you'll build this

[`LayerSelector::set_budget`](../src/simulcast.rs),
[`wants_keyframe`](../src/simulcast.rs), [`on_packet`](../src/simulcast.rs)
and [`selected_bitrate`](../src/simulcast.rs) in
[simulcast.rs](../src/simulcast.rs). The budget arrives from V4's estimator
via the wired core; the decisions feed V2's rewriter as shown above.

This doc unlocks V3's **Done when ALL true** ([SPEC.md](../SPEC.md)): right
layer for the budget, keyframe-gated up-switch with a clearing owed-flag,
immediate down-switch, deselected layers dropped (with `skip`), and
switch invisibility — proven by `picks_highest_fitting_layer`,
`up_switch_waits_for_keyframe`, `down_switch_is_immediate`,
`deselected_layer_is_dropped`.
