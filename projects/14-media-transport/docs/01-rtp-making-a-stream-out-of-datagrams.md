# RTP: Making a Stream out of Datagrams — From First Principles

> How 12 bytes of header turn lonely UDP datagrams into an ordered, timed,
> attributable media stream — and how a frame bigger than a packet gets split
> and stitched back. No prior RTP knowledge assumed.
>
> Prepares you for **V1** in [SPEC.md](../SPEC.md). Anchored to
> [rtp.rs](../src/rtp.rs) (`RtpHeader::parse` / `write`,
> `Packetizer::packetize`, `depacketize` — the `todo!()`s you're about to
> fill) and the wired call sites in [session.rs](../src/session.rs).

---

## 0. The one sentence to hold onto

**RTP adds exactly the facts a datagram is missing — order (sequence number),
time (media timestamp), framing (marker bit), and identity (SSRC) — and
nothing else.** Every field in the 12-byte header exists because without it,
some specific failure becomes undetectable.

---

## 1. The problem: what a bare datagram can't tell you

Suppose the sender just chopped a video stream into UDP payloads and fired
them off. The receiver gets a datagram of encoded bytes. Now ask it basic
questions:

| Question the receiver must answer | Without a header… | The header field that answers it |
| --- | --- | --- |
| Did I miss anything? Is this datagram #4 or #5? | Unknowable — loss is silent on UDP | **sequence number** (16-bit, +1 per packet) |
| These 5 datagrams — same frame, or 5 frames? | Unknowable | **timestamp** (same value across one frame's packets) |
| Is this the *last* piece of the frame, i.e. can I decode now? | You'd wait for the next frame to start — one frame of extra latency | **marker bit** (set on the frame's final packet) |
| When should this frame be shown, relative to the previous one? | Unknowable — arrival time is polluted by network jitter | **timestamp** again (it's the media clock, not the wire clock) |
| Who sent this? (An open UDP port takes bytes from *anyone*.) | Any stray/spoofed source pollutes your stream | **SSRC** (32-bit random stream id) |
| How do I decode the payload? | Guess | **payload type** (7-bit codec label) |

Every downstream vertical consumes these answers: the jitter buffer (V2)
orders by *sequence*, groups frames by *timestamp*, releases on *marker*;
NACK (V3) names losses by *sequence*; the SSRC gates who's allowed into the
buffer at all. V1 is the floor everything stands on.

## 2. The header, byte by byte

This layout is already in the rustdoc of [rtp.rs](../src/rtp.rs) — it's the
contract, not the solution. The solution is *your* bounds-checked parser.

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|V=2|P|X|  CC   |M|     PT      |       sequence number         |   bytes 0–3
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           timestamp                           |   bytes 4–7
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                             SSRC                              |   bytes 8–11
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        CSRC (0..=15) …                        |   4 bytes each
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

All multi-byte fields are **big-endian** (network byte order). The pieces:

- **V** (2 bits) — always `2`. The scaffold already rejects anything else
  ([rtp.rs](../src/rtp.rs), `BadVersion`).
- **P, X** (1 bit each) — padding / extension flags. Rare; parse them, don't
  trip over them.
- **CC** (4 bits) — *count of CSRC entries* that follow the fixed 12 bytes.
  This is the header's one variable-length knob, and therefore its one
  parser trap: a datagram can *claim* `CC=15` (60 more bytes) while being 13
  bytes long. **The claim must be checked against `buf.len()` before any
  read** — that's the "clean error, never a panic or OOB read" criterion.
- **M** (1 bit) — the marker: "this is the frame's last packet."
- **PT** (7 bits) — payload type. Dynamic codecs like H.264 use the 96–127
  range agreed out-of-band (ffmpeg defaults to 96).
- **sequence** (16 bits) — +1 per *packet*, starts at a random value, wraps
  at 65535 → 0. (Why random? So an attacker off-path can't trivially guess
  valid sequence numbers; same reason TCP randomizes its initial sequence.)
- **timestamp** (32 bits) — the *media* clock. For video: 90,000 ticks per
  second. Also starts at a random offset.
- **SSRC** (32 bits) — random per stream; [session.rs](../src/session.rs)
  already does `let ssrc: u32 = rand::random()`.

## 3. Sequence vs. timestamp: two clocks that advance independently

This is the concept the SPEC names explicitly, and the most common beginner
confusion. Trace one second of 30 fps video, where each frame needs 3 packets:

```
                 frame 0          frame 1          frame 2
               ┌───┬───┬───┐    ┌───┬───┬───┐    ┌───┬───┬───┐
   sequence:   │100│101│102│    │103│104│105│    │106│107│108│   +1 per PACKET
               └───┴───┴───┘    └───┴───┴───┘    └───┴───┴───┘
   timestamp:    9000 (all 3)     12000 (all 3)    15000 (all 3) +3000 per FRAME
   marker:        0   0   1        0   0   1        0   0   1    last pkt only
```

- The **sequence** advances once per packet, because its job is *transport*:
  detecting loss and restoring order. (90000 / 30 fps = **3000 ticks** per
  frame — that's where the +3000 comes from.)
- The **timestamp** advances once per *sampling instant*, because its job is
  *presentation*: "all these packets belong to the moment t=9000, show them
  together." Three packets, one moment, one timestamp.

If one field did both jobs, you couldn't tell "3 packets of one frame" from
"3 one-packet frames" — and the jitter buffer's frame-grouping (V2) and the
depacketizer's reassembly contract both die.

**Trap (from [CONCEPTS.md](../CONCEPTS.md) Card 1):** the timestamp is *not*
wall-clock. It's a counter on a 90 kHz clock with a random origin. It tells
you frames are 3000 ticks = 33.3 ms *apart*; it cannot tell you what time it
is. Mapping media time to wall time is a separate mechanism (RTCP Sender
Reports — project 17's problem).

## 4. Fragmentation: when one frame doesn't fit in one packet

### 4.1 The budget

An Ethernet-class path carries at most **1500 bytes** of IP packet (the MTU).
Subtract the overheads:

```
   1500  path MTU
   -  20  IPv4 header
   -   8  UDP header
   = 1472  max UDP payload  →  the scaffold's cfg.mtu is in this ballpark
   -  12  RTP header
   = 1460  media budget per single-NAL packet
   -   2  FU-A headers (indicator + header, §4.3)
   = 1458  media budget per fragment
```

Meanwhile a 1.5 Mbps / 30 fps stream averages 1.5e6 / 30 / 8 ≈ **6.25 KB per
frame** — and keyframes are far bigger. Frames not fitting in packets is the
*normal case*, not the edge case. At 1458 bytes per fragment, that average
frame needs **5 fragments**.

### 4.2 Why not let IP fragment for you?

IP *can* split an oversized datagram into fragments and reassemble them in
the receiver's kernel. Letting it is a classic mistake:

| IP fragmentation | Application-layer fragmentation (FU-A) |
| --- | --- |
| Lose one fragment → kernel silently discards the *whole* datagram after a timeout. You lose 6 KB because 1.4 KB went missing. | Lose one fragment → you lost exactly that fragment. It has its own sequence number, so V2 sees the gap and V3 can NACK *just it*. |
| Fragments have no per-fragment identity visible to you — nothing to NACK. | Every fragment is a first-class RTP packet. |
| Many middleboxes/firewalls drop IP fragments outright. | Every packet is a normal, small UDP datagram. |
| Reassembly state lives in kernel buffers you don't control. | Reassembly is your `depacketize`, with your error handling. |

That's why the SPEC's criterion says **"MTU-respecting: no emitted packet
exceeds the configured MTU"** — the whole point is that IP never needs to
fragment.

### 4.3 FU-A: the H.264 fragmentation format

H.264 encodes video as **NAL units** (Network Abstraction Layer units) — for
this project, "NAL unit" ≈ "the encoded blob you were handed", and the
synthetic source in [media.rs](../src/media.rs) emits access units to carry.
Each NAL starts with a 1-byte header (forbidden bit, importance bits `NRI`,
and a 5-bit `type`). The packetization rules (RFC 6184) say:

- **Fits in the budget** → ship the NAL as-is; the RTP payload *is* the NAL.
  (The receiver knows it's a single NAL because the payload's first byte has
  type 1–23.)
- **Too big** → split into **FU-A** fragments (type 28). Each fragment's
  payload is 2 header bytes + a slice of the NAL:

```
   original NAL:   [ NAL hdr (1B) | ...........6250 bytes of data........... ]

   fragment 1:  [ FU indicator | FU header S=1,E=0 | first 1458 bytes  ]
   fragment 2:  [ FU indicator | FU header S=0,E=0 | next 1458 bytes   ]
   ...
   fragment 5:  [ FU indicator | FU header S=0,E=1 | the remainder     ]

   FU indicator = the NAL's forbidden+NRI bits, with type replaced by 28
   FU header    = S (start) | E (end) | reserved | the ORIGINAL NAL's 5-bit type
```

The original NAL header byte is *not* sent verbatim — it's reconstructed on
reassembly from the FU indicator's NRI bits + the FU header's type bits.
The **S** and **E** bits are what make loss *detectable*: a receiver holding
fragments with an S but no E (or a sequence gap in between) knows the frame
is incomplete and must report an error — never emit corrupt bytes. That's the
SPEC's fourth Done-when criterion, verbatim.

### 4.4 One frame through the packetizer

Putting it all together — a 6250-byte access unit, MTU budget 1460,
`sequence` currently at 2000, frame timestamp 90000:

| pkt | sequence | timestamp | marker | payload |
| --- | --- | --- | --- | --- |
| 1 | 2000 | 90000 | 0 | FU-A, S=1, bytes 1–1458 of NAL data |
| 2 | 2001 | 90000 | 0 | FU-A, bytes 1459–2916 |
| 3 | 2002 | 90000 | 0 | FU-A, bytes 2917–4374 |
| 4 | 2003 | 90000 | 0 | FU-A, bytes 4375–5832 |
| 5 | 2004 | 90000 | **1** | FU-A, E=1, bytes 5833–6249 |

Consecutive sequences, shared timestamp, marker only on the last — that table
*is* the `packet_sequence_and_marker_are_correct` test. And `depacketize` of
those five payloads, in order, must reproduce the original 6250 bytes exactly
— that's `fragmented_frame_reassembles`.

## 5. The design space you own (and where to stop reading)

The wire format is fixed; these decisions are yours:

- **The MTU number itself.** `TransportConfig.mtu` in
  [session.rs](../src/session.rs) is configurable. What do you assume about
  the path — 1500-class Ethernet? A conservative 1200 like WebRTC defaults
  to (to survive tunnels/VPNs)? Document the budget in `docs/14-design.md`.
- **How `parse` reports "the bytes lie".** The scaffold's
  [error.rs](../src/error.rs) gives you `Truncated` and `BadVersion`;
  deciding what's checked *before* each read — CC count vs. remaining length,
  FU runs with missing S/E — is the actual craft of V1. A parser on an open
  UDP port is a parser of hostile input, always.
- **How the packetizer walks the access unit** — how you slice, how you carry
  the sequence across packets (it wraps at 65535 — `u16` arithmetic is your
  friend), how you decide marker placement when a frame is one packet vs.
  many.

That last bullet is the `todo!()` in `Packetizer::packetize` — the exact
slicing-and-stamping loop is the build, and this doc stops at its door.
`/hint 14` for graduated nudges, `/quest` to build it with acceptance tests.

## 6. Mental-model summary

| Field / mechanism | Job | Failure without it |
| --- | --- | --- |
| sequence (per packet) | Loss detection + ordering | Gaps invisible; reorder uncorrectable |
| timestamp (per frame) | Grouping + presentation timing | Can't tell frame boundaries or pacing |
| marker | "Frame complete, decode now" | A frame of extra latency waiting for the next timestamp |
| SSRC | Stream identity on an open port | Any stray sender pollutes the buffer |
| FU-A S/E bits | Per-fragment loss visibility | Incomplete frames emitted as corrupt bytes |
| MTU-respecting packetizer | Keep IP from fragmenting | One lost fragment silently kills a whole frame, unNACKably |

**Where you'll build this:** the four `todo!()`s in [rtp.rs](../src/rtp.rs) —
`RtpHeader::parse`, `RtpHeader::write`, `Packetizer::packetize`,
`depacketize` (plus `RtpPacket::parse`/`serialize` glue). They unlock all
five of V1's **Done when ALL true** boxes: header round-trip + truncation
safety, >MTU fragmentation/reassembly, sequence/timestamp/marker invariants,
incomplete-frame detection, and MTU respect. The sender loop hits
`packetize` as its first panic; the receiver loop hits `RtpPacket::parse` —
that's your worklist order.
