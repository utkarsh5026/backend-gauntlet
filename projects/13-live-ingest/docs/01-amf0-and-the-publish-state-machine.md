# How AMF0 and the Publish State Machine Work — RTMP's Control Language

> A ground-up guide to what flows *inside* the messages V1 reassembles: **AMF0**,
> the typed binary serialization RTMP uses for its commands, and the precise
> call-and-response dance (`connect` → `createStream` → `publish`) a broadcaster
> performs before it will send a single frame of media. No prior knowledge of
> serialization formats, RPC, or RTMP internals assumed — but read
> [00-rtmp-chunk-stream.md](00-rtmp-chunk-stream.md) first: this doc starts where
> that one ends, at a reassembled `Message` with `type_id = 20`.
>
> This prepares you for **V2** in [SPEC.md](../SPEC.md) — "AMF0 commands + the
> publish state machine" — anchored to [amf.rs](../src/amf.rs) (the
> `decode`/`encode` `todo!()`s and the `Amf0` enum) and
> [session.rs](../src/session.rs) (the `State` enum, wired accept loop, and the
> `Session::handle` `todo!()`). The wire format and the message sequence are
> public protocol facts and are taught in full; the decoder loop and the state
> machine's code are yours to write.

---

## 0. The one sentence to hold onto

**Past the handshake, RTMP is an RPC conversation serialized in AMF0, and your
session is a state machine that only lets a connection graduate to "sending
media" after it has answered the right commands in the right order — with the
stream-key check standing as the auth gate on that final transition.**

Two halves: a *codec* (pure functions over bytes — `amf.rs`) and a *protocol
brain* (stateful, per-connection — `session.rs`). The scaffold splits them so
the codec is exhaustively testable without a socket.

---

## 1. The problem: the encoder is waiting for you to speak

After V1's handshake, `ffmpeg` sends a command message and then… waits. It will
not send video. It will not send audio. It is waiting for a specific reply, and
if your reply is malformed, misordered, or missing, it disconnects — usually
silently. The failure modes are all invisible without understanding the dance:

| You do | Broadcaster does | What you see |
| --- | --- | --- |
| Never reply to `connect` | waits ~10 s, gives up | "handshake works, then nothing" |
| Reply with wrong transaction id | ignores your reply, waits | same silent hang |
| Reply to `createStream` without a stream id | can't address its `publish` | disconnect |
| Skip the `onStatus NetStream.Publish.Start` | never starts media | connected forever, zero bytes of video |
| Accept media without checking the key | **stream hijacking** | works great, right up until it's an incident |

That last row is the security half of V2. The others are the protocol half.
Both live in [`Session::handle`](../src/session.rs).

---

## 2. AMF0: a 1-byte type marker, then the value

AMF0 ("Action Message Format", from Flash) is how command payloads are encoded.
It's a classic **TLV-ish typed serialization**: read one marker byte, and the
marker tells you how to read what follows. Your
[`marker` module](../src/amf.rs) lists the five types a publish flow uses:

| marker | type | encoding of the value |
| --- | --- | --- |
| `0x00` | number | 8 bytes: IEEE-754 f64, **big-endian** (yes, *every* number — stream ids, booleans-as-flags, all f64) |
| `0x01` | boolean | 1 byte: `0x00` false, else true |
| `0x02` | string | 2-byte big-endian length, then that many UTF-8 bytes |
| `0x03` | object | repeated `⟨2-byte key length, key bytes, value⟩` pairs, terminated by an **empty key** (`00 00`) followed by marker `0x09` |
| `0x05` | null | nothing |

Note the asymmetry that trips everyone: object **keys** are *not* full AMF0
strings — they have no `0x02` marker, just the u16 length + bytes. Only the
*values* carry markers. The terminator is therefore unambiguous: a zero-length
key (`00 00`) can't be real, so `00 00 09` ends the object.

### 2.1 A worked decode: a real `connect`, by hand

Here is a minimal `connect` command body — 35 bytes, the kind V1 hands you in a
`Message { type_id: 20, .. }` payload (byte values verified):

```
02 00 07 63 6f 6e 6e 65 63 74   string(7) "connect"        ← command name
00 3f f0 00 00 00 00 00 00      number 1.0                 ← transaction id
03                              object {
   00 03 61 70 70               key(3) "app"
   02 00 04 6c 69 76 65           string(4) "live"         ←   app: "live"
   00 00 09                     } (empty key + OBJECT_END)
```

Read it aloud: marker `02` ⇒ string; length `00 07` ⇒ 7 bytes; `63 6f 6e 6e 65
63 74` is ASCII `connect`. Marker `00` ⇒ number; `3f f0 00 00 00 00 00 00` is
f64 `1.0`. Marker `03` ⇒ object; key length `00 03`, key `app`; value marker
`02`, string `live`; then `00 00 09` closes it. (A real `ffmpeg` connect object
also carries `flashVer`, `tcUrl`, audio/video codec flags — more pairs, same
shape.)

That's the whole format. A command message body is simply **several AMF0 values
concatenated**: name, transaction id, command object (or null), then arguments
— which is why [`amf::decode`](../src/amf.rs) returns `Vec<Amf0>`, not a single
value.

### 2.2 Why round-trip is the correctness bar

You need both directions: `decode` for the client's commands, `encode` for your
`_result`/`onStatus` replies. The property `decode(encode(v)) == v` (for the
five supported types) is the cheapest strong test that both are right — any
length miscount, endianness slip, or terminator bug breaks identity on some
input. That's exactly the SPEC's `amf0_roundtrips_publish_command` proof, and
it's why the scaffold keeps `amf.rs` free of I/O: pure `&[u8]` → `Vec<Amf0>`
functions are property-testable in a tight loop.

And the same hostile-input rule as V1 applies — a declared string length must
be checked against the bytes actually remaining *before* you slice. One decoder
row in the horizontal security checklist ("AMF string/object sizes are
range-checked") is entirely about this.

---

## 3. The publish sequence: who says what, and which reply unblocks what

Here's the full dance a publisher performs, with your lines marked. Transaction
ids pair a `_result` to the call it answers (the client picks them; you echo
them back):

```
ffmpeg / OBS                              your Session
     │                                          │ state: Connected
     │  connect("live")  txn=1                  │
     │────────────────────────────────────────▶│
     │                                          │ (Window Ack Size, Set Peer BW —
     │◀────────────────────────────────────────│  bookkeeping, then:)
     │  _result  txn=1  {code:                  │
     │◀──"NetConnection.Connect.Success"}───────│ state: AppConnected
     │                                          │
     │  releaseStream("testkey"), FCPublish     │ ← optional/legacy: ignore or
     │────────────────────────────────────────▶│   ack loosely, never crash
     │                                          │
     │  createStream()  txn=4                   │
     │────────────────────────────────────────▶│
     │  _result  txn=4  stream_id=1             │
     │◀────────────────────────────────────────│ state: StreamCreated
     │                                          │
     │  publish("testkey", "live")  on stream 1 │
     │────────────────────────────────────────▶│ ── authorize("testkey") ──┐
     │  onStatus {level:"status", code:         │                     pass │
     │◀──"NetStream.Publish.Start"}─────────────│ state: Publishing  ◀─────┘
     │                                          │        (fail ⇒ refuse + close)
     │  @setDataFrame/onMetaData, then           │
     │  AUDIO (8) / VIDEO (9) messages forever  │
     │────────────────────────────────────────▶│ → V3 packager
```

Three replies gate three client behaviors: `_result` to `connect` unblocks
everything else; `_result` to `createStream` gives the client the **message
stream id** it will stamp on its media messages (recall `Message::stream_id`
from V1); `onStatus NetStream.Publish.Start` is the green light that actually
starts media flowing.

The middle rows — `releaseStream`, `FCPublish`, and whatever else a client you've
never met sends — are the forward-compatibility lesson: **unknown commands are
ignored, not fatal**. An ingest that panics on a command it doesn't know breaks
the day OBS ships a new version.

---

## 4. Why a state machine, and why the gate is where it is

You could write `handle()` as "if command is X, reply Y" with no memory. Here's
what each missing piece of state permits:

| Missing rule | Attack / failure it permits |
| --- | --- |
| Media only accepted in `Publishing` | anyone who completes a handshake can pump bytes into your packager — unauthenticated resource burn, and garbage into V3 |
| `publish` requires `authorize(key)` | **anyone can broadcast as anyone**: an open ingest lets a stranger hijack a channel by guessing its URL path |
| `publish` only valid from `StreamCreated` | commands arriving out of order corrupt half-initialized state (which stream id? which key?) |
| Duplicate `publish` handled deliberately | a re-sent `publish` mid-stream re-opens/clobbers the live window |

So the scaffold's [`State`](../src/session.rs) enum is the design:
`Connected → AppConnected → StreamCreated → Publishing`, transitions driven
only by correctly-answered commands, media (type 8/9) rejected in any state but
the last. The SPEC's `media_rejected_before_publish` test is this table's first
row made executable.

The key check itself is wired for you:
[`LiveRegistry::authorize`](../src/live.rs) (a static allow-list from
`STREAM_KEYS`; the TODO there notes a real deployment would verify a signed
token). What V2 owns is *where it's called and what refusal does*: refuse ⇒
close the session — and per the horizontal checklist, **never log the raw key**
(it's a credential; log a hash or prefix). Note the URL shape:
`rtmp://host/live/testkey` — "live" arrives as the `app` in `connect`,
"testkey" as the argument to `publish`. The key *is* the password; that's why
OBS's settings page calls it exactly that.

For the rejection rows there's a real decision the SPEC explicitly asks you to
document: is an out-of-order or duplicate command **rejected** (error the
session) or **ignored** (drop and continue)? Real encoders reconnect mid-dance
and occasionally resend; too strict and you break interop, too loose and the
table above comes back. Decide, document in `docs/13-design.md`, test it.

---

## 5. The handoff to V3: mining the first media messages for setup

The moment `Publishing` begins, media messages arrive — but the *first* ones are
special. RTMP media payloads are **FLV tags**, and H.264/AAC each send a
**sequence header** before any real frames:

| FLV tag | first bytes say | carries | maps to scaffold |
| --- | --- | --- | --- |
| video, `AVCPacketType = 0` | "AVC sequence header" | the `AVCDecoderConfigurationRecord` — SPS/PPS, i.e. the **`avcC`** | `CodecConfig::avc_decoder_config` |
| audio, `AACPacketType = 0` | "AAC sequence header" | the **AudioSpecificConfig** (~2 bytes: profile, sample rate, channels) | `CodecConfig::aac_audio_specific_config` |
| video, `AVCPacketType = 1` | "NALUs" | actual coded frames | `fmp4::Sample` (V3) |

Why does setup arrive once, first, instead of per frame? Because a decoder
can't decode frame one without it (SPS/PPS describe resolution, profile, and
entropy-coding parameters), and repeating it per-frame wastes bytes on
something that never changes mid-encode. Your V2 dispatcher extracts these
sequence headers into a [`CodecConfig`](../src/fmp4.rs) — the exact input V3's
`build_init` needs — and turns every *subsequent* tag into a `Sample`. That
extraction is the fourth V2 Done-when box, and it's the seam where this
vertical hands off to the next: [02-live-fmp4-remuxing.md](02-live-fmp4-remuxing.md).

---

## 6. The design space you're deciding

The protocol is fixed; these choices are yours (record them in
`docs/13-design.md`):

- **Decoder shape.** A cursor you advance vs. slicing `&[u8]` recursively —
  either works; the invariant is that every length is bounds-checked before use
  and object decoding terminates (a buffer of endless key/value pairs must hit
  the end-of-input error, not spin).
- **Reply plumbing.** Your replies must go back *through the chunk layer* —
  encoded AMF0 wrapped in a type-20 message, chunked for writing. How much of a
  chunk *writer* you build (you control the size, so single-chunk messages are
  legal) is a scope decision V1 left open.
- **Rejection policy.** §4's reject-vs-ignore call, per command.
- **How much of `connect`'s object you honor.** `app` matters (it's half the
  URL); most of the rest (`flashVer`, capability flags) an ingest can ignore.
  Knowing what you can *not* implement is part of reading a legacy protocol.

That's the door this doc stops at. `/hint` for graduated nudges, `/quest` to
build the vertical against acceptance tests.

---

## 7. Mental model summary

| Concept | Hold onto |
| --- | --- |
| AMF0 | 1-byte marker then value; numbers are BE f64, strings u16-length-prefixed, objects end at `00 00 09` |
| Object keys | length-prefixed but *unmarked* — only values carry type markers |
| Command body | several AMF0 values concatenated: name, transaction id (f64), object/null, args ⇒ `decode` returns `Vec<Amf0>` |
| Round-trip | `decode∘encode = identity` is the codec's correctness bar (and it's I/O-free so you can property-test it) |
| The dance | `connect`→`_result` · `createStream`→`_result(stream_id)` · `publish(key)`→`onStatus Publish.Start` — each reply unblocks the next client step |
| Transaction id | echo the client's number back so it can pair reply to call |
| State machine | media accepted only in `Publishing`; wrong-order/duplicate commands handled deliberately (documented reject-or-ignore) |
| Auth gate | `authorize(key)` guards the `StreamCreated → Publishing` edge; refuse ⇒ close; never log the raw key |
| Unknown commands | ignored, never fatal — forward compatibility with encoders you haven't met |
| Sequence headers | first video/audio tags carry `avcC` / AudioSpecificConfig — the `CodecConfig` V3 needs, arriving once, up front |

## 8. Where you'll build this

Three `todo!()`s across two files:

- [`amf::decode`](../src/amf.rs) and [`amf::encode`](../src/amf.rs) — the codec
  (§2), pure functions, property-test them hard.
- [`Session::handle`](../src/session.rs) — the dispatcher and state machine
  (§3–5): route by `type_id`, run the dance, gate on
  [`LiveRegistry::authorize`](../src/live.rs), extract the codec config, feed
  samples onward.

The accept loop, session lifecycle, and stream-teardown (`mark_ended`,
`registry.close`) are already wired around you in
[session.rs](../src/session.rs).

This doc unlocks V2's **Done when ALL true** ([SPEC.md](../SPEC.md)): AMF0
round-trips + never panics on truncated input · full publish sequence with a
real broadcaster · media gated on state · codec config extracted · unknown key
refused. Proof: `amf0_roundtrips_publish_command`,
`media_rejected_before_publish`, and a live `ffmpeg` publish reaching the media
phase, noted in `docs/13-design.md`.
