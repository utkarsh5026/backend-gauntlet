# How RTMP's Chunk Stream Works — Parsing a Stateful Binary Wire

> A ground-up guide to the protocol floor of this project: the RTMP **handshake**
> and the **chunk stream** — how one TCP connection carries interleaved video,
> audio, and control messages, and why the parser you're about to write must be
> *stateful by design*. No prior knowledge of RTMP, binary protocols, or media
> streaming assumed.
>
> This prepares you for **V1** in [SPEC.md](../SPEC.md) — "RTMP handshake +
> chunk-stream reader" — anchored to [rtmp.rs](../src/rtmp.rs): the
> `handshake()` `todo!()`, the `ChunkStreamReader::read_message()` `todo!()`, and
> the `ChunkStreamCtx` / `Message` types the scaffold already gives you. It
> teaches the *wire format* (which is public protocol spec, not the solution);
> the parsing code that walks it is yours to write.

---

## 0. The one sentence to hold onto

**RTMP shreds every message into small chunks so that big video frames can't
block small audio frames on the shared TCP connection — and it makes the chunk
headers *delta-compressed against earlier chunks*, so your parser must carry
state per chunk stream to reassemble anything at all.**

Two ideas in one sentence: *why chunks exist* (head-of-line blocking) and *why
your reader holds a `HashMap` of contexts* (header compression). Everything in
V1 is one of those two.

---

## 1. The problem: one TCP connection, three kinds of traffic

A broadcaster (OBS, `ffmpeg`) opens **one** TCP connection to your server and
needs to send, continuously and simultaneously:

| Traffic | Typical size | Cadence | Tolerance for delay |
| --- | --- | --- | --- |
| Video frames (H.264) | 5–200 KB (keyframes are huge) | ~30/s | seconds of buffer downstream |
| Audio frames (AAC) | ~200–800 bytes | ~47/s | **audible stutter past ~50 ms** |
| Control messages (commands, acks) | tens of bytes | occasional | protocol stalls if late |

Now the naive design: send each message whole, one after another, on the socket.

```
naive wire:  [====== 200 KB keyframe ======][audio][audio][audio][command]...
                        ▲
             everything queues behind this
```

TCP is a byte pipe — nothing overtakes anything. At a 4 Mbps uplink, a 200 KB
keyframe occupies the wire for **~410 ms** (200 × 1024 × 8 bits ÷ 4,000,000 bps).
Every audio frame produced during those 410 ms waits in line. That's
**head-of-line blocking inside your own connection**, and it lands on the
viewer's ears as a stutter every time a keyframe goes by.

You can't fix it with more sockets (RTMP predates that idea, and multiple TCP
connections fight each other's congestion control). The fix is the same one
HTTP/2 rediscovered 15 years later: **multiplexing** — cut every message into
small pieces and interleave the pieces.

```
chunked wire: [vid₁][vid₂][audio][vid₃][audio][vid₄][cmd][vid₅][audio]...
                                  ▲
              audio slips between video chunks; max wait = one chunk
```

With the default chunk size of **128 bytes**, that 200 KB keyframe becomes
**1600 chunks** (204,800 ÷ 128), and an audio frame waits at most one chunk —
microseconds, not 410 ms. The cost moved from the wire to *you*: something must
now put those 1600 pieces back together, correctly, while other messages'
chunks arrive interleaved between them. That something is
[`ChunkStreamReader::read_message`](../src/rtmp.rs) — your V1.

---

## 2. Before any chunks: the handshake

An RTMP connection opens with a fixed three-leg exchange. It's not negotiation —
almost nothing is decided — it's a *liveness and byte-correctness gate*:

```
client                                server (you)
  │  C0: 1 byte, version 0x03          │
  │  C1: 1536 bytes                    │
  │──────────────────────────────────▶│
  │                                    │  S0: 1 byte, 0x03
  │                                    │  S1: 1536 bytes (your random block)
  │                                    │  S2: 1536 bytes (echo of C1)
  │◀──────────────────────────────────│
  │  C2: 1536 bytes (echo of your S1)  │
  │──────────────────────────────────▶│
  │        ... chunk stream ...        │
```

Each 1536-byte block is `4-byte time + 4-byte zero + 1528 random bytes`
(4 + 4 + 1528 = 1536, the scaffold's `HANDSHAKE_SIZE`). The random bytes exist
so each side can prove the other actually *read* what it sent: S2 must echo C1's
random block, C2 must echo S1's. A proxy that blindly forwards, or a server
that's off by one byte, fails the echo — and the client hangs up.

That's why the SPEC's first Done-when box says handshake completion **is** the
proof: `ffmpeg` will not send a single command to a server whose S2 is wrong.
There is no "partially working" handshake. (There's also a "complex handshake"
variant with HMAC digests hidden in those blocks, used by Flash-era DRM;
`ffmpeg`/OBS accept the simple one. Note which you implement in
`docs/13-design.md` — the scaffold's TODO says the same.)

---

## 3. The chunk format: a header that shrinks as the stream gets boring

After the handshake, everything is chunks. Each chunk is:

```
+-------------+------------------+----------------------+----------+
| basic hdr   | message header   | extended timestamp   | payload  |
| 1–3 bytes   | 0/3/7/11 bytes   | 0 or 4 bytes         | ≤ chunk  |
|             | (depends on fmt) | (only if ts=0xFFFFFF)|   size   |
+-------------+------------------+----------------------+----------+
```

### 3.1 The basic header: fmt + chunk stream id

The first byte packs two fields: the top 2 bits are the **format** (`fmt`,
0–3), the low 6 bits the **chunk stream id** (csid) — which *lane* this chunk
belongs to. Values 0 and 1 in the csid field are escapes for 2- and 3-byte
forms (ids beyond 63); a media session mostly uses small ids that fit in one
byte.

| byte | fmt | csid | meaning |
| --- | --- | --- | --- |
| `0x04` | 0 | 4 | full header follows, lane 4 |
| `0x44` | 1 | 4 | 7-byte header, lane 4 |
| `0x84` | 2 | 4 | 3-byte header, lane 4 |
| `0xC4` | 3 | 4 | **no** header, lane 4 |

A **chunk stream** (lane) is not a message stream — it's a header-compression
context. The encoder typically puts commands on one lane, audio on another,
video on a third, so each lane's traffic is self-similar (that's what makes the
compression below work).

### 3.2 The message header: four formats, progressive inheritance

This is the heart of V1. A full message header (fmt 0) carries four fields.
Each higher fmt *omits* fields, which the decoder must **inherit from the
previous chunk on the same csid**:

| fmt | bytes | carries | inherits from previous chunk on this csid |
| --- | --- | --- | --- |
| 0 | 11 | timestamp (24-bit, absolute), length (24), type id (8), msg stream id (32, **little**-endian — the one LE field in the protocol) | nothing |
| 1 | 7 | timestamp **delta** (24), length (24), type id (8) | msg stream id |
| 2 | 3 | timestamp **delta** (24) | length, type id, msg stream id |
| 3 | 0 | nothing | everything (delta too) |

Why bother? Look at what a steady stream actually sends. Audio at 47 frames/s
is a run of messages that differ *only* in timestamp — and by the *same* delta
each time. After one fmt-0 (establish everything) and one fmt-1 or fmt-2
(establish the delta), every subsequent audio message needs only fmt 3: **1
byte of header per chunk**. Compare a design that repeated the full header on
every 128-byte chunk: 12 bytes of header per 128 of payload is **9.4%
overhead**; fmt 3 is **0.8%**. On a 24/7 ingest at scale, that's the difference
the design buys.

The price: **the wire is meaningless without memory.** A fmt-3 chunk is *pure
payload* — its timestamp, length, type, everything comes from state you kept.
That's exactly the [`ChunkStreamCtx`](../src/rtmp.rs) struct in the scaffold
(one per csid, in the reader's `HashMap`), and it's why `read_message` can't be
a pure function of the bytes.

### 3.3 fmt 3 does double duty — the subtle part

fmt 3 means two different things depending on context, and your reader must
distinguish them:

1. **Continuation:** the previous chunk on this csid did *not* complete its
   message (payload accumulated < declared length). This fmt-3 chunk is the
   next slice of the *same* message. No new timestamp applies.
2. **Repeat:** the previous message on this csid *was* complete. This fmt-3
   chunk starts a *new* message identical in every header field — including
   applying the timestamp delta again.

The scaffold's `ChunkStreamCtx.partial` buffer is what tells them apart:
non-empty ⇒ continuation, empty ⇒ new message.

### 3.4 Extended timestamps

The header's timestamp field is 24 bits, so it maxes out at `0xFFFFFF` =
16,777,215 ms ≈ **4.66 hours** — a real broadcast blows past that. The escape:
a field value of exactly `0xFFFFFF` means "the real value is in 4 extra bytes
right after the message header." Your reader must check for the sentinel in
*both* absolute timestamps (fmt 0) and deltas (fmt 1/2) — and RTMP's ugliest
corner: a fmt-3 chunk whose inherited timestamp was extended *also* carries the
4 extra bytes. (Encoders disagree on that last rule; it's a classic interop
trap. Note how you handle it in `docs/13-design.md`.)

### 3.5 `Set Chunk Size`: the ground shifts mid-stream

128 bytes is only the *starting* chunk size. Almost the first thing a real
encoder sends is a **Set Chunk Size** control message (type id 1, see
[`msg_type::SET_CHUNK_SIZE`](../src/rtmp.rs)) raising it to something like
4096 — at 128 bytes, chunk-header overhead on video is silly. From the moment
you *finish reassembling* that message, every subsequent chunk's payload
boundary is different. The scaffold's `set_chunk_size()` (with its clamp —
see §6) is wired; *when* to apply it inside your read loop is part of the
puzzle.

---

## 4. A worked trace: one video message, three chunks

A 300-byte video message (type 9) on csid 4, message stream 1, timestamp 40 ms,
default 128-byte chunks. 300 = 128 + 128 + 44, so three chunks:

```
chunk 1: 04 | 00 00 28 | 00 01 2C | 09 | 01 00 00 00 | <128 payload bytes>
          │    ts=40      len=300   typ    msid=1 (LE!)
          └ fmt=0, csid=4                              partial: 128/300

chunk 2: C4 | <128 payload bytes>
          └ fmt=3, csid=4 → continuation (partial non-empty)
                                                       partial: 256/300

chunk 3: C4 | <44 payload bytes>   ← min(chunk_size, remaining) = 44
          └ fmt=3, csid=4 → continuation completes it  partial: 300/300 ✓
```

Reassembly yields one `Message { type_id: 9, stream_id: 1, timestamp: 40,
payload: [300 bytes] }` — and note chunk 3 reads only 44 bytes, not 128: the
last chunk of a message is *short*. Reading a full `chunk_size` there would
swallow the next chunk's basic header. This is the single most common V1 bug.

Now the next video frame arrives, same size, 33 ms later, as
`44 | 00 00 21 | 00 01 2C | 09 | ...` — fmt 1 (`0x44`): timestamp *delta* 33
(`0x000021`), length and type repeated, stream id inherited. And the frame
after that, if it's also 300 bytes: just `84 | 00 00 21 | ...` (fmt 2, delta
only) — or even bare `C4` (fmt 3: same delta re-applied). Meanwhile audio
chunks on csid 6 interleave freely between all of these, tracked by their *own*
`ChunkStreamCtx`. That interleaving — two lanes' state advancing independently
— is what `chunk_header_fmt_inheritance` and `reassembles_multichunk_message`
in your test list must prove.

---

## 5. Where the messages go

`read_message` hands each completed [`Message`](../src/rtmp.rs) up to
[`Session::handle`](../src/session.rs) (V2), routed by `type_id`:

| type id | constant | what it is | who consumes it |
| --- | --- | --- | --- |
| 1 | `SET_CHUNK_SIZE` | control: new chunk size | the reader itself |
| 3 / 5 / 6 | `ACK` / `WINDOW_ACK_SIZE` / `SET_PEER_BANDWIDTH` | flow-control bookkeeping | session (mostly ignorable for ingest) |
| 8 / 9 | `AUDIO` / `VIDEO` | FLV-tagged media | V2 → V3 packager |
| 18 / 20 | `AMF0_DATA` / `AMF0_COMMAND` | metadata / RPC | V2 (`amf.rs` + `session.rs`) |

V1's contract is clean: *bytes in, whole typed messages out*. Nothing above V1
ever sees a chunk boundary — the SPEC's second Done-when box says exactly that.

---

## 6. Hostile input: an open port takes bytes from anyone

Port 1935 is an unauthenticated TCP listener. Before the first credential check
(V2's stream key), *anyone* can send *anything*. Every declared length in the
protocol is an attack surface:

| field the wire declares | naive reader does | attacker sends | result |
| --- | --- | --- | --- |
| message length (24-bit) | `Vec::with_capacity(len)` | `0xFFFFFF` × 64 csids | ~1 GB allocated from one connection |
| chunk size | trusts it | `0x7FFFFFFF` | one "chunk" swallows the connection |
| basic-header csid escapes | reads N more bytes | truncated stream | read past end / hang forever |

The discipline: **range-check every declared length before allocating or
slicing, and treat violation as a session-ending error** — a clean
`Err(AppError)`, never a panic, never an unbounded allocation. The scaffold
already hands you the two guards: `max_message_size` on the reader (checked
against the declared length *before* you extend `partial`) and the clamp inside
`set_chunk_size`. The SPEC's last V1 box and the `malformed_chunks_never_panic`
fuzz test are this row of the design, and the horizontal security checklist
repeats it for a reason: this is the part of V1 that's production-shaped, not
protocol-trivia-shaped.

---

## 7. The design space you're deciding in `read_message`

The wire format is fixed; the *reader's shape* is yours. The decisions worth
making deliberately (and recording in `docs/13-design.md`):

- **Read granularity.** Byte-exact reads per field vs. a buffered reader you
  slice from. One is simpler to reason about; the other does fewer syscalls.
  Either can be correct — the trap is a buffer that reads *past* a chunk into
  the next one and loses its place.
- **Where `Set Chunk Size` is absorbed.** The scaffold's TODO suggests the
  reader absorbs it (returning only non-control messages, or handing it up —
  either way, applied at the right instant). Decide whose job it is and keep it
  in one place.
- **What "corrupt" means.** A fmt-1 chunk on a csid you've never seen has
  nothing to inherit. Error, or tolerate with zeroed context? Real encoders
  should never do it; fuzzed bytes will. Your error path is as load-bearing as
  your happy path.
- **When contexts die.** A `HashMap<u32, ChunkStreamCtx>` that grows per csid
  is fine for the handful a real encoder uses — but a hostile peer can mint
  csids. Is the map bounded?

Each of these is a few lines of code and a real judgment call. That's the V1
learning — which is why this doc stops here. When you're stuck mid-build,
`/hint` gives graduated nudges; `/quest` runs the vertical end-to-end with
acceptance tests written up front.

---

## 8. Mental model summary

| Concept | Hold onto |
| --- | --- |
| Why chunks | One TCP pipe + big frames = head-of-line blocking; 128-byte chunks cap any message's wait at ~1 chunk |
| Handshake | C0/C1 ↔ S0/S1/S2 ↔ C2; echo of 1528 random bytes; completion with real `ffmpeg` **is** the correctness proof |
| csid | A header-compression *lane*, not a message stream; each lane has independent inherited state |
| fmt 0→3 | 11 → 7 → 3 → 0 header bytes; each omitted field inherited from the previous chunk on that csid |
| fmt 3 | Continuation if a message is mid-assembly on that csid; full repeat (delta re-applied) otherwise |
| Extended ts | Field value `0xFFFFFF` (≈4.66 h in ms) ⇒ real value in 4 extra bytes; applies to deltas too |
| Set Chunk Size | Mid-stream boundary change; applies from the moment the message is reassembled |
| Statefulness | `HashMap<csid, ChunkStreamCtx>` + current chunk size — the wire is undecodable without it |
| Hostile input | Range-check every declared length before allocating; violations end the session, cleanly |

## 9. Where you'll build this

Both `todo!()`s live in [rtmp.rs](../src/rtmp.rs):

- `handshake()` — the C0/C1 ↔ S0/S1/S2 ↔ C2 exchange (§2).
- `ChunkStreamReader::read_message()` — basic header → fmt-dispatched message
  header → inheritance → payload accumulation → completed `Message` (§3–4),
  with the `max_message_size` guard (§6).

They're called from the already-wired [`Session::run`](../src/session.rs), so
the moment they work, a real `ffmpeg -f flv rtmp://localhost:1935/live/testkey`
gets past the handshake and its `connect` command lands in `Session::handle` —
V2's doorstep.

This doc unlocks V1's **Done when ALL true** ([SPEC.md](../SPEC.md)): handshake
completes with a real broadcaster · multi-chunk reassembly · fmt 0–3
inheritance · extended timestamps + mid-stream Set Chunk Size · malformed input
ends the session cleanly. Proof: `reassembles_multichunk_message`,
`chunk_header_fmt_inheritance`, `malformed_chunks_never_panic`, and a live
`ffmpeg` handshake noted in `docs/13-design.md`.
