# The Wire & the Guardrails — Fundamentals Woven Through This Project

> The backend fundamentals the SPEC's horizontal checklist and ⚡ rapid-fire
> round grade: how three protocols share one UDP port, what stands between an
> open port and an authenticated media path, PLI vs FIR, bounded-everything,
> the metrics that tell you an SFU is healthy, and the honest SRTP scope
> call. No prior knowledge assumed; shorter than the vertical docs because
> several of these are already wired — the point is *understanding* them.
>
> Anchored to [wire.rs](../src/wire.rs) (wired), [pump.rs](../src/pump.rs)
> (wired), [sfu.rs](../src/sfu.rs) (wired core), [metrics.rs](../src/metrics.rs),
> [admin.rs](../src/admin.rs) and the [SPEC's](../SPEC.md) horizontal checklist.

---

## 0. The one sentence to hold onto

**Everything on the media port arrives as anonymous UDP bytes; the system's
safety is a chain of cheap, total judgments — classify by first byte, parse
with bounds checks, accept media only from ICE-authenticated addresses, cap
every table and window — so a hostile or broken peer degrades itself, never
the process.**

---

## 1. One port, three protocols: the RFC 7983 demux

WebRTC **bundles** STUN, RTP and RTCP onto a single UDP 5-tuple. Why cram
three protocols down one port instead of using three?

- **One NAT binding.** Every distinct port is another hole to punch and keep
  alive through the peer's NAT. One port = one ICE negotiation, one
  keepalive, one thing to break.
- **One authenticated path.** ICE nominates an address pair *per transport*.
  Bundle everything and one nomination secures all of it.

The price is that every datagram needs a verdict *before* any parser runs.
RFC 7983's observation: the protocols' first bytes can't collide —

| First byte | Protocol | Why it can't collide |
|---|---|---|
| `0x00–0x03` | **STUN** | Top two bits are always `00` (doc [01](01-ice-stun-reachability.md)) |
| `0x80–0xBF` | **RTP or RTCP** | Both start with version bits `10` |
| `0x14–0x3F` | DTLS | (out of scope here — dropped) |
| anything else | garbage | dropped |

RTP vs RTCP share the `10xxxxxx` band, so the *second* byte disambiguates:
RTCP packet types live in 192–223; anything else in the band is RTP. All of
this is wired — [`classify`](../src/wire.rs) is a dozen lines and
[pump.rs](../src/pump.rs) dispatches on it:

```
recv_from ──▶ classify ──▶ Stun ──▶ Sfu::handle_stun   (V1)
                       ├─▶ Rtp  ──▶ Sfu::handle_rtp    (V2+V3)
                       ├─▶ Rtcp ──▶ Sfu::handle_rtcp   (V4+V2)
                       └─▶ Unknown ─▶ drop, count, move on
```

Note the pump's error posture, stated in its module doc: **media-plane errors
are dropped, not fatal** — a malformed datagram costs one packet, never the
loop. On an open port, "crash on bad input" is a remote kill switch.

## 2. Every parser is bounds-checked (the security floor)

Three parsers touch attacker-controlled bytes: STUN attribute TLVs (yours,
V1), the RTP header ([`RtpView::new`](../src/wire.rs) — wired, note it
refuses runt datagrams before any accessor indexes), and RTCP length words.
The rule is uniform: **every length field is a hostile number until checked
against the actual buffer.** An oversized claim, a truncated datagram, an
attribute that overruns — each is a clean `Err`/drop
([error.rs](../src/error.rs) has `Truncated`/`BadMagic`/`Malformed` for
exactly this), never a panic, never an out-of-bounds read, never an
attacker-sized allocation. The V1 criteria make this testable ("total on
garbage" — property tests feeding random bytes); the horizontal checklist
extends the same demand to RTP and RTCP.

## 3. ICE-gated media: the port's authentication line

The subtle rapid-fire item: after ICE completes, **RTP/RTCP is accepted only
from addresses that were nominated via an integrity-checked STUN check.**
A datagram from any other source — a scanner, a spoofer, a stale peer — is
ignored, not forwarded.

Derive why this matters: UDP source addresses are trivially forgeable, and
the SFU *forwards* what it accepts. Without the gate, anyone who learns your
media port could inject packets into a call (garbage into viewers' decoders)
or use your SFU as an amplifier. With it, the sequence is:

```
 signaling (HTTP): hand out ufrag/pwd        ── who may even attempt
 STUN + MESSAGE-INTEGRITY: prove you hold pwd ── authenticate the check
 nomination: bind peer ⇄ source address       ── authorize the address
 media: addr → peer lookup on every datagram  ── enforce, per packet
```

That makes ICE not just reachability but the **auth layer of the port** —
the reason V1's "an unauthenticated check never nominates" criterion is a
security property, not pedantry.

### The honest scope call: SRTP

Real WebRTC also *encrypts* media: DTLS-SRTP (a TLS-family handshake over
the media path yielding keys that encrypt every RTP payload). This project
deliberately scopes it **out** — it's a large cryptographic subsystem with
little forwarding insight — and the SPEC requires `docs/15-design.md` to say
so *explicitly* rather than silently ship an unencrypted media path. Naming
what you didn't build is part of the engineering: address-gating
authenticates the *source*; without SRTP, an on-path observer can still read
payloads. Know exactly which property you have.

## 4. PLI vs FIR: the two keyframe requests

Both are RTCP feedback messages that ask a sender for a keyframe; the
distinction is *what you're claiming*:

| | **PLI** (Picture Loss Indication) | **FIR** (Full Intra Request) |
|---|---|---|
| Semantics | "I lost your picture — I can't decode from here" | "Produce a fresh keyframe *now*" (a command) |
| Classic use | Loss recovery; the polite, common case | Stream entry points: a new viewer joins, a switcher cuts inputs |
| Sender's latitude | May answer with cheaper recovery than a full keyframe | Must emit a full intra frame |

For a simulcast up-switch (doc [03](03-simulcast-layer-selection.md)) either
works in practice — most SFUs send PLI — but your design doc names the
choice. The discipline matters more than the pick: **one request per pending
switch** (`wants_keyframe` gating), because keyframes are 5–10× normal frame
size and a request storm inflates the very bitrate you're managing.

## 5. Bounded everything

Recurring gauntlet theme, sharpest here because the port is open and the
process is the media plane for *every* room:

| Resource | Bound | Who enforces |
|---|---|---|
| Rewriter NACK-translation window | fixed-size (V2 criterion) | your [`Rewriter`](../src/forward.rs) |
| Rooms / peers per room | `MAX_ROOMS` / `MAX_PEERS_PER_ROOM` ([`SfuConfig`](../src/sfu.rs), from [.env](../.env.example)) | wired signaling — a join flood gets HTTP errors, not an OOM |
| Datagram size | pump's 2048-byte recv buffer | wired [pump.rs](../src/pump.rs) |
| Retransmit/history caches | capped | wherever you add one |

The invariant, verbatim from the SPEC: *a join flood or a chatty peer
degrades itself, never the process.* The boss fight verifies it brutally —
RSS flat through a 50-subscriber join storm and a 5-minute-vs-5-hour room.
The design smell to watch for as you build V2–V4: any per-peer collection
without an explicit cap is a bug you haven't met yet.

## 6. Observability: what an SFU's health looks like

You can't watch pixels — the process never decodes any. Health is expressed
in the quantities the SPEC's checklist names ([metrics.rs](../src/metrics.rs)
declares them; [admin.rs](../src/admin.rs) serves `/metrics`, `/status`,
`/healthz`, `/readyz`):

- **The amplification ratio** — RTP received vs forwarded. The fan-out *is*
  the workload (doc [00](00-why-an-sfu.md)); this pair is the SFU's
  throughput identity, and a ratio that sags below subscriber count means
  forwarding is silently failing.
- **Drops by reason** — a deselected-layer drop is *policy* (normal, high
  volume); a malformed/unauthenticated drop is *defense* (should be near
  zero from honest peers). One undifferentiated counter hides an attack
  inside normal operation.
- **Lifecycle counters** — STUN messages, ICE nominations, layer switches
  (up/down), keyframe requests, NACKs translated. Rates tell stories: PLI
  rate ≈ up-switch rate is healthy; PLI ≫ switches is a storm.
- **The adaptation pair** — *estimated vs selected bitrate* per subscriber.
  Estimated tracks the link (V4); selected snaps between layer steps (V3);
  watching them together is literally watching the control loop work — and
  it's how the boss fight's convergence criterion gets measured.
- **Per-peer tracing spans** (room + peer id/SSRC) for join/leave, ICE
  nominated, layer switch, keyframe request — and **never log payload
  bytes**: today they're media (volume), the day you add SRTP they're
  ciphertext (nonsense), and either way they're user content (privacy).

**Graceful shutdown** rounds out the checklist: SIGTERM → stop forwarding,
drain in-flight HTTP, tear down peers — no half-open sessions. The
watch-channel plumbing exists in [main.rs](../src/main.rs) and
[pump.rs](../src/pump.rs); making the drain complete is checklist work.

## 7. Mental model summary

| Guardrail | One-line rule | Failure it prevents |
|---|---|---|
| RFC 7983 demux | First byte(s) decide the parser | Wrong parser on hostile bytes |
| Total parsers | Every length checked before use; errors drop one packet | Remote panic/OOM = kill switch |
| ICE-gated media | No nomination, no forwarding | Injection, spoofing, amplification |
| SRTP scope honesty | Unencrypted payload, stated in the design doc | Believing you have a property you don't |
| PLI discipline | One keyframe request per cause | Keyframe storms inflating bitrate |
| Bounded everything | Every per-peer structure has a cap | One peer OOMing every room |
| Drops by reason | Policy drops ≠ defense drops | Attacks hidden inside normal ops |
| estimated-vs-selected | Watch the loop, per subscriber | Flying the adaptation blind |

## 8. Where this lands

No single module — this doc *is* the horizontal checklist
([SPEC.md](../SPEC.md)): real-stack interop, RTCP both ways, graceful
shutdown, bounds-checked parsers, ICE-gated media with the SRTP scope named,
bounded tables, and the tracing/counters/gauges lists. Most boxes get
checked as a side effect of building V1–V4 *with these rules in mind*;
the rest (shutdown drain, metrics wiring) are small, honest chores. The ⚡
rapid-fire round in [CONCEPTS.md](../CONCEPTS.md) is the self-test for
whether this doc stuck.
