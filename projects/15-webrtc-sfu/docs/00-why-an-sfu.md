# Why an SFU Exists — Mesh vs MCU vs SFU, From First Principles

> The framing idea for this whole project: how one publisher's video reaches
> fifty viewers without melting anyone's uplink or the server's CPU. No prior
> knowledge of conferencing systems assumed.
>
> This is Card 0 of [CONCEPTS.md](../CONCEPTS.md) — not a vertical you build,
> but the idea that explains *why* the four verticals you will build
> ([ice.rs](../src/ice.rs), [forward.rs](../src/forward.rs),
> [simulcast.rs](../src/simulcast.rs), [bwe.rs](../src/bwe.rs)) exist at all.

---

## 0. The one sentence to hold onto

**An SFU forwards the publisher's already-encoded packets — payload untouched —
choosing per subscriber *what* to forward; its CPU cost is per-*packet*, never
per-*pixel*, and that asymmetry is why 50-person calls exist.**

Everything else in this project is what "forwards" turns out to hide.

---

## 1. The problem: one publisher, many viewers, all on different networks

Picture a video call. Alice turns on her camera. Her browser encodes the video
at, say, ~1.5 Mbps and now those bytes must reach every other participant —
one on fibre, one on hotel wifi, one on a phone that just dropped to 3G.

There are exactly three places the "copying" can happen, and the whole design
space of conferencing is those three choices:

```
   MESH                      MCU                          SFU
   everyone → everyone       server decodes & re-mixes    server forwards packets

   A ──▶ B                   A ──▶ ┌─────────┐ ──▶ B      A ──▶ ┌────────┐ ──▶ B
   A ──▶ C                        │ decode  │ ──▶ C           │ route + │ ──▶ C
   A ──▶ D                   B ──▶ │ compose │ ──▶ D      B ──▶ │ rewrite │ ──▶ D
   (× every publisher)       C ──▶ │ re-encode│ …         C ──▶ │ headers │ …
                             D ──▶ └─────────┘            D ──▶ └────────┘
```

## 2. Why the two obvious answers fail

### Mesh: everyone sends everyone a copy

No server at all — each participant uploads one copy of their stream to each
other participant. The cost lands on the **publisher's uplink**, and it scales
with the room. At ~1.5 Mbps per stream:

| Participants | Uplink per person | Total streams in flight |
|---:|---:|---:|
| 4  | 3 × 1.5 = **4.5 Mbps**  | 12 |
| 10 | 9 × 1.5 = **13.5 Mbps** | 90 |
| 50 | 49 × 1.5 = **73.5 Mbps** | 2,450 |

A typical home connection uploads 5–20 Mbps. Mesh is genuinely great at 2–3
people (lowest possible latency, no server), and **dead at ~4** — which is why
1:1 calls often run peer-to-peer while group calls never do.

### MCU: the server decodes everyone and re-encodes one picture

The Multipoint Control Unit fixes the network math completely: every
participant uploads **one** stream and downloads **one** stream, because the
server decodes all inputs, composites them into one grid picture, and
re-encodes a fresh stream (possibly tailored) for each viewer. The network is
happy. The problems:

| Cost | Why it's structural |
|---|---|
| **CPU per pixel.** | Video encoding is the most expensive common workload a server can run. Decoding N streams and re-encoding M outputs burns a core-per-call; server cost scales with *pixels processed*, not packets moved. |
| **A latency hop in every frame.** | Every frame must be fully decoded, composited, and re-encoded before it can leave. That's an unavoidable added delay in the one application where latency is the product. |
| **Quality ceiling.** | Every viewer gets the server's re-encode — a second lossy generation — no matter how good their link is. |

MCUs still exist (recording, telephony gateways, giant-grid broadcast views),
but you cannot build "cheap 50-person calls" on cores-per-call economics.

## 3. The SFU: forward, don't transcode

The Selective Forwarding Unit takes the MCU's topology (star: everyone talks
to the server) but refuses to touch the media:

- The publisher uploads their stream **once**.
- The SFU **forwards the already-encoded RTP packets** to each subscriber —
  the payload bytes leave exactly as they arrived.
- All adaptation happens by **selection** (which packets to forward — V3) and
  **rewriting** (a few header fields — V2), never by re-encoding.

Per participant: one upload, one download per stream they watch. Per server:
the cost of moving a packet is a route lookup + a 12-byte header patch +
a `send_to` — the same cost whether the frame inside is 240p or 4K. **CPU is
O(packets), not O(pixels).** That is the entire economic argument, and the
boss fight's "No transcode tax: CPU stays flat as bitrate rises" criterion in
[SPEC.md](../SPEC.md) exists to make you *prove* it.

### The workload this creates: fan-out amplification

The SFU trades the mesh's upload amplification for **egress amplification at
the server** — one ingress packet becomes up to N egress packets. With this
project's numbers (a ~1.5 Mbps publisher at ~1200-byte payloads ≈ 156
packets/sec in), a 50-subscriber room is ~7,800 packets/sec out *as an upper
bound* — and the per-packet forwarding path is the hot loop. That's why the
boss fight measures "ingress packet → egress `send_to`" p99, and why the
per-packet work must stay tiny.

## 4. What "forward" hides (the map of this project)

"Just relay the packets" decomposes into four hard sub-problems — exactly the
four verticals:

| Hidden problem | Why a dumb relay fails | Vertical |
|---|---|---|
| The browser is behind NAT — it can't even *reach* you until an authenticated path is negotiated. | Packets to a private address go nowhere; unsolicited inbound is dropped. | **V1** · [ice.rs](../src/ice.rs) |
| Each subscriber must see one *continuous* stream even as the SFU drops packets under them and switches origins. | A sequence gap reads as network loss; an SSRC jump reads as a broken stream. | **V2** · [forward.rs](../src/forward.rs) |
| Different links need different quality — without decoding a pixel. | One encoding either melts the 3G viewer or starves the fibre one. | **V3** · [simulcast.rs](../src/simulcast.rs) |
| Nobody tells you a subscriber's downlink capacity — you must estimate it from feedback. | Sending above capacity builds a queue that delays *everything* on that viewer's link. | **V4** · [bwe.rs](../src/bwe.rs) |

The plumbing around them — the muxed UDP socket, the RFC 7983 first-byte
demux, the RTP header accessors, the room/peer bookkeeping — is wired for you
in [wire.rs](../src/wire.rs), [pump.rs](../src/pump.rs) and
[sfu.rs](../src/sfu.rs), because those parts are mechanical. The four
primitives above are exactly what you'd otherwise hand to
`webrtc-rs`/`libwebrtc`, which is why here you build them.

## 5. Mental model summary

| | Mesh | MCU | SFU |
|---|---|---|---|
| Publisher uplink | N−1 copies (dies ~4 people) | 1 copy | 1 copy |
| Server CPU | none | **per pixel** (decode+re-encode) | **per packet** (route+rewrite) |
| Added latency | lowest | decode→compose→encode hop | ~a route lookup |
| Per-viewer quality adaptation | none | perfect (re-encode each) | by *selection* among publisher-provided layers |
| Who runs it | 1:1 calls | recording/gateways | Zoom, Meet, Teams, Discord, LiveKit, Jitsi, mediasoup — everyone |

You own Card 0 when you can sketch the three topologies with their costs at a
whiteboard, and explain what "adaptation by selection and rewriting, never by
re-encoding" predicts about the SFU's CPU as subscribers join (flat) and as
bitrate rises (flat) — the two plots the boss fight demands.

## 6. Where to go next

Read the docs in order — they follow the SPEC's suggested order of attack:
reachability first ([01](01-ice-stun-reachability.md)), then the forwarding
primitive ([02](02-per-subscriber-rtp-rewriting.md)), then what to forward
([03](03-simulcast-layer-selection.md)), then the number that drives the
choice ([04](04-bandwidth-estimation.md)), then the wire-level fundamentals
woven through it all ([05](05-the-wire-and-the-guardrails.md)).
