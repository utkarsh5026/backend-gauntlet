# Per-Subscriber RTP Rewriting — One Continuous Stream Out of a Switching Origin

> The primitive at the heart of an SFU: how the server drops packets and swaps
> which stream feeds a viewer, while the viewer's browser sees one perfectly
> continuous stream. No prior RTP knowledge assumed beyond what's summarized
> here (project 14 built it; the refresher below is self-contained).
>
> Prepares you for **V2** in [SPEC.md](../SPEC.md) — the [`Rewriter`] in
> [forward.rs](../src/forward.rs) (`rewrite`, `skip`, `to_origin_seq` — all
> `todo!()`), operating through the wired zero-copy header accessors in
> [wire.rs](../src/wire.rs).

---

## 0. The one sentence to hold onto

**The SFU's edits — dropped packets, switched origins — must be *invisible*
downstream, so each subscriber gets a private rewriter that owns their
outbound identity: one stable SSRC, sequence numbers that advance by exactly
1 no matter what was skipped, timestamps monotonic across switches — and
enough memory of the mapping to route a NACK back to the origin packet.**

---

## 1. Thirty seconds of RTP (the fields that matter here)

Every RTP packet carries a 12-byte header the SFU reads and rewrites via
[`RtpView`](../src/wire.rs):

| Field | Size | Meaning to a receiver |
|---|---|---|
| **SSRC** | 32 bits | "Which stream is this?" A new SSRC = a *different stream*. |
| **sequence number** | 16 bits | Per-packet counter, +1 each packet, wraps 65535 → 0. Gaps mean *the network lost something*. |
| **timestamp** | 32 bits | Media clock ticks — *when* this belongs in playback. Going backwards is nonsense to a decoder. |
| marker, payload type | 1 + 7 bits | Marker flags the last packet of a frame; PT names the codec. |

The receiving browser is not a passive sink. Its **jitter buffer** actively
interprets those fields: a sequence gap ⇒ "packet lost, send a **NACK**
(negative acknowledgment) asking for a resend"; an SSRC change ⇒ "new stream,
reset everything"; a timestamp jump backwards ⇒ undefined weirdness. The
receiver *trusts the header semantics completely* — that trust is what the
rewriter exploits and must never betray.

## 2. The problem: "forward the packet" is a lie the moment you don't

The SFU **deliberately** doesn't forward everything. Two everyday cases:

1. **Deselected simulcast layers** (V3): the publisher sends three encodings;
   this subscriber gets exactly one. Two-thirds of origin packets are dropped
   *on purpose, per subscriber*.
2. **Origin switches** (V3 again): when the subscriber's bandwidth changes,
   the SFU changes *which* encoding feeds them — a different SSRC, an
   unrelated sequence range, a different timestamp base.

Forward the raw headers through either of those and here's what the
subscriber's browser concludes:

| SFU's editorial act | Raw-forwarded header effect | What the browser does |
|---|---|---|
| Drop a deselected-layer packet | Sequence gap | Declares loss; **NACKs a packet you never sent it** — pointless retransmit traffic that grows with drop rate |
| Switch origin layer | SSRC changes mid-stream | Treats it as a brand-new stream; playback breaks/resets |
| Switch origin layer | Sequence jumps to an unrelated range | Massive perceived loss or reordering chaos |
| Switch origin layer | Timestamp jumps (maybe backwards) | Frozen or garbled video — *sometimes*, depending how the bases happen to differ |

That last row is the trap [CONCEPTS.md](../CONCEPTS.md) flags: rewrite the
sequence but forward timestamps raw, and video freezes only *when the two
origins' timestamp bases differ enough* — an intermittent bug that teaches you
all three fields must be rewritten **coherently**.

So: gapless outbound sequencing is a **correctness property**, not cosmetics.
The wire between SFU and subscriber must read as "a perfect network delivering
one stream", where the only gaps are *real* network loss on that last hop —
which the subscriber correctly NACKs.

## 3. The rewriter: a private outbound line per subscriber

One [`Rewriter`](../src/forward.rs) lives **per subscriber** (not per origin
— that placement is exactly what makes a layer switch invisible: the origin
changes, the rewriter doesn't). It owns:

- the subscriber's **stable outbound SSRC** (assigned at subscribe time by the
  wired [`Sfu::subscribe`](../src/sfu.rs), returned by
  [`out_ssrc()`](../src/forward.rs)),
- whatever running state keeps the outbound sequence contiguous and the
  timestamp monotonic (that's the `_state: ()` placeholder — your design),
- a **bounded** history mapping outbound → origin sequence, for NACKs.

### Worked example: drops become invisible

Origin layer SSRC `0xAAAA0001`, subscriber's outbound SSRC `0x51B0001`.
The SFU skips two packets (deselected/pacing), forwards the rest:

| Origin seq | SFU decision | Outbound seq | Subscriber perceives |
|---:|---|---:|---|
| 5978 | forward → `rewrite` | 4126 | packet 4126 ✓ |
| 5979 | forward → `rewrite` | 4127 | packet 4127 ✓ |
| 5980 | **drop** → `skip` | — | *nothing happened* |
| 5981 | forward → `rewrite` | 4128 | packet 4128 ✓ — no gap! |
| 5982 | **drop** → `skip` | — | *nothing happened* |
| 5983 | forward → `rewrite` | 4129 | packet 4129 ✓ |

Note the contract split in the API: [`rewrite`](../src/forward.rs) stamps and
records a forwarded packet; [`skip`](../src/forward.rs) accounts for a
not-forwarded one. The caller (wired core + V3's selector) promises to call
exactly one of them per origin packet; the rewriter's job is that the
outbound line stays `…4126, 4127, 4128, 4129…` regardless of the mix.

### Worked example: an origin switch is just "the input changed shape"

Mid-call, V3 switches this subscriber from the low layer to the high layer.
Input side, *everything* jumps; output side, *nothing* may:

```
          INPUT (what feeds the rewriter)         OUTPUT (what the subscriber sees)
  before  ssrc AAAA0001, seq …5983, ts ~T₁        ssrc 51B0001, seq …4129, ts smooth
  after   ssrc BBBB0002, seq 9101…, ts ~T₂ ≠ T₁   ssrc 51B0001, seq 4130…, ts still smooth
                    ▲ all three discontinuous              ▲ all three continuous
```

The rewriter must *detect* the new origin (the SSRC changed) and **rebase**:
new origin sequence range mapped onto "next outbound = 4130", new origin
timestamp base mapped so playback time keeps advancing plausibly. What
"plausibly" means — how big the timestamp step across the seam should be — is
one of your design decisions (§5).

## 4. NACK translation: reliability routed across a rewrite

The subscriber's NACKs name **outbound** sequence numbers — the only ones it
knows. Suppose it NACKs 4128 (from the table above; real loss on its last
hop). The SFU's retransmit path needs the **origin** packet, and 4128 was
origin seq 5981. Someone has to remember that pair. That someone is the
rewriter: [`to_origin_seq(4128) == Some(5981)`](../src/forward.rs).

Three requirements shape it:

1. **Across the 16-bit wrap.** Sequences wrap 65535 → 0 (…65534, 65535, 0,
   1… is *contiguous*). A mapping recorded at outbound 65534 must still
   resolve after the counter wraps — the explicit `across_the_wrap` test
   case. Sixteen-bit "distance" is modular; naïve `<`/`>` comparisons lie
   near the wrap (project 14's lesson, back again).
2. **Bounded.** The history is a **fixed-size window**. A 5-hour stream and a
   5-minute stream hold the same state; a subscriber that never NACKs grows
   nothing. This is the repo-wide "bounded everything on an open port" rule —
   the scaffold comment calls it an OOM guard.
3. **Aging out is correct behavior, not failure.** `to_origin_seq` returns
   `None` for a sequence older than the window: a packet that old is past its
   playout deadline anyway, so retransmitting it would waste the very
   bandwidth you're managing (project 14's deadline logic). The window size
   is therefore a *policy*: roughly, how much retransmit-useful past is worth
   remembering — you'll pick a size and defend it in `docs/15-design.md`.

## 5. The design space (what's yours to decide)

The scaffold comment in [forward.rs](../src/forward.rs) sketches the state
*categories* (an origin↔outbound offset, last outbound seq/ts, current origin
SSRC, a bounded mapping ring). The decisions that remain — the interesting
part — include:

- **Offset vs. explicit map (or both).** Contiguity needs to know "what's the
  next outbound seq"; NACK translation needs per-packet pairs. Do those share
  one structure or two? What does `skip` touch?
- **Timestamp rebasing policy.** Across a switch, the new origin's clock is
  unrelated. What step do you stamp across the seam so playback neither
  stalls nor lurches — and what do you do on the *first* packet ever?
- **Wrap-safe lookup.** How the window is indexed so wrap-adjacent sequences
  resolve correctly and stale entries are evicted for free.
- **Window size.** Big enough to cover a realistic NACK round-trip at your
  packet rates; small enough to honor bounded-memory. Name the number, defend
  it.

Every subscriber's rewriter is fully independent — `two_rewriters_are_independent`
exists because shared state here is how one viewer's bad link degrades
another's stream. If you find your design wanting shared mutable state
between subscribers, that's the smell.

When you reach for a data structure and it feels like the answer, that's the
`/hint` and `/quest` boundary — this doc stops at the door.

## 6. Mental model summary

| Concept | The rule | Why |
|---|---|---|
| Outbound SSRC | One per subscriber, never changes | SSRC change = "new stream" to a jitter buffer |
| Outbound sequence | +1 per *forwarded* packet, wrapping; `skip` leaves no gap | Gap = loss = pointless NACK |
| Outbound timestamp | Monotonic, plausible across origin switches | Coherent rewriting — the intermittent-freeze trap |
| NACK translation | outbound seq → origin seq, wrap-safe, windowed | Reliability must survive the rewrite |
| Aged-out mapping | `None`, by design | Past the playout deadline; retransmit would be waste |
| Memory | Fixed-size, always | Open-port OOM guard; hour-long calls cost what minute-long ones do |
| Placement | Per subscriber, not per origin | Makes V3's layer switch invisible *by construction* |

## 7. Where you'll build this

[`Rewriter::rewrite`](../src/forward.rs), [`Rewriter::skip`](../src/forward.rs)
and [`Rewriter::to_origin_seq`](../src/forward.rs) in
[forward.rs](../src/forward.rs); the header patching goes through
[`RtpView::set_ssrc` / `set_sequence` / `set_timestamp`](../src/wire.rs),
already wired. The origin-SSRC → subscribers routing table is bookkeeping the
wired [sfu.rs](../src/sfu.rs) core keeps — your module is only the per-line
rewriting brain.

This doc unlocks V2's **Done when ALL true** ([SPEC.md](../SPEC.md)):
contiguous outbound stream across skips, continuity across an origin switch,
wrap-safe NACK translation with aging, subscriber isolation, and bounded
state — proven by `rewrite_is_contiguous`, `rewrite_survives_origin_switch`,
`nack_translates_back` (+ `across_the_wrap`), `two_rewriters_are_independent`.
