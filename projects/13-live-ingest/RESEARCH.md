# Live Video Ingest & Low-Latency Delivery Internals: A Systems Engineer's Deep Dive

## TL;DR
The end-to-end path is: an RTMP contribution feed (H.264/AAC in FLV framing over a TCP chunk stream) is demuxed, the elementary bitstreams are repackaged **without re-encoding** into CMAF fragmented-MP4, and those fragments are exposed as LL-HLS parts discovered by clients via blocking playlist reload. For a from-scratch Rust learner, the highest-value primitives are: a correct chunk-stream reader (fmt 0-3 headers, delta timestamps, the fmt-3 extended-timestamp trap), an AMF0 command state machine (connect → createStream → publish), an AVCC→fMP4 remuxer that gets `tfdt` baseMediaDecodeTime and signed composition-time offsets right, and an in-memory rolling window that serves a spec-correct media playlist with `_HLS_msn`/`_HLS_part` long-poll semantics. Plain HLS sits at 15-30 s glass-to-glass; LL-HLS and LL-DASH reach 2-5 s; WebRTC/WHIP reach 200-500 ms but sacrifice CDN economics. RTMP is aging (TCP head-of-line blocking, 32-bit ms timestamps, single-track) but remains a dominant contribution on-ramp in 2026 (Haivision's 2024 survey found RTMP used by 56% of broadcast professionals, second only to SRT at 68%); SRT/RIST/WHIP are the successors and MoQ is the frontier that is demo-ready but not yet default infrastructure.

## Key Findings
- **The chunk stream is the whole trick in RTMP.** A multi-megabyte video frame is sliced into ≤ChunkSize pieces (default 128 bytes, almost always renegotiated up to 4096+), interleaved with audio/control on other chunk-stream IDs, and reassembled by message stream ID. Get the four header formats and delta-timestamp compression right and RTMP "just works"; get the fmt-3 extended-timestamp rule wrong and you desync on long-running or high-timestamp streams.
- **The complex handshake is HMAC-SHA256 over 1536-byte blobs** keyed by the "Genuine Adobe Flash Media Server/Player 001" constants plus a fixed 32/30-byte binary tail. A packager acting as a server can usually accept the *simple* handshake (echo the bytes) and interoperate with almost everything.
- **Remuxing is a bitstream copy, not a transcode.** You extract SPS/PPS from the AVCDecoderConfigurationRecord once (into the `avcC` box in the init segment), then for each FLV video tag emit a `moof`+`mdat` with the AVCC-length-prefixed NALUs copied verbatim. The only arithmetic is timeline math: `baseMediaDecodeTime` and per-sample `composition_time_offset`.
- **B-frames force signed composition offsets.** FLV carries `CompositionTime` (signed 24-bit ms); in fMP4 you need a `trun` **version 1** with signed `sample_composition_time_offset`, because PTS < DTS for reordered frames.
- **The `data_offset` back-patch is the classic fMP4 bug.** `trun.data_offset` points from the start of the enclosing `moof` to the first sample byte in `mdat`; you can't know it until the `moof` is fully serialized, so you write a placeholder and patch it.
- **LL-HLS's core is a long-poll, not a push.** The client asks `?_HLS_msn=M&_HLS_part=N`; the server *holds the connection open* until that part exists, then returns the updated playlist. HTTP/2 push was the original mechanism and Apple **removed** it (Sept 2023); preload hints are now the single normative mechanism.
- **The "3× target-duration" rule and PART-HOLD-BACK are hard invariants.** A live playlist must retain ≥3 target durations of media; PART-HOLD-BACK must be ≥3× PART-TARGET. Violate these and Apple's `mediastreamvalidator` fails you and real players stall.
- **CDN request amplification is the scaling wall.** parts/sec × viewers held-open requests; cache keys **must** include `_HLS_msn`/`_HLS_part`; origin shield + request coalescing collapses a thundering herd of identical part requests into one origin fetch.
- **The latency floor below ~2 s is physics, not protocol.** A ~1 s encoder pipeline (GOP + B-frame reference + entropy coding), 200-400 ms decoder pre-roll, RTT×jitter, and CDN overhead are irreducible for HTTP streaming; WebRTC beats it only by discarding the segment model.
- **In 2026: RTMP still dominates contribution ingest; SRT is the professional public-internet standard (68% adoption in Haivision's 2024 report); WHIP (RFC 9725, Standards Track, March 2025) is a finished standard for sub-second WebRTC ingest; MoQ is feature-complete-enough for demos but not default infrastructure.**

## Details

### 1. Fundamentals: live ingest vs VOD, and the latency budget

VOD packaging is an offline batch job: you have the whole file, you can two-pass encode, index precisely, and write a static playlist. Live is a *streaming* problem with three hard differences: (1) the media timeline is open-ended and only the tail exists; (2) you have a wall-clock deadline — a packager that falls behind the encoder must drop or the buffer grows unbounded; (3) discovery — clients must learn about new media with minimum delay. Everything distinctive about live ingest and low-latency delivery flows from these three constraints.

**The glass-to-glass budget.** Latency is the sum of stage costs, and it helps to write it down as a document before coding. A representative breakdown (from practitioner budgets):

```
Stage                         plain HLS     LL-HLS        WebRTC
Camera + ISP capture          ~80 ms        ~80 ms        ~80 ms
Encoder (GOP + lookahead)     ~1 s          ~1 s          ~100 ms (no lookahead)
Contribution (RTMP/SRT/RTP)   RTT-bound     RTT-bound     RTP: ~0
Packager (segmenting)         segment dur   part dur      none (per-frame)
Client hold-back              3× segment    PART-HOLD-BACK jitter buffer ~50-200 ms
Decoder + display             ~50 ms        ~50 ms        ~50 ms
────────────────────────────────────────────────────────────────
Total glass-to-glass          ~15-30 s      ~2-5 s        ~0.2-0.5 s
```

The single biggest lever is the client hold-back: plain HLS mandates the player start ~3 segments from the live edge, so 6-10 s segments → ~20-30 s. LL-HLS shrinks the fetchable unit from a segment to a ~200 ms part and replaces poll-and-wait with long-poll, recovering the playlist-refresh and buffer contributions. WebRTC removes segmentation entirely — each encoded frame is RTP-packetized and sent immediately, which is the one and only reason it reaches sub-second where HLS cannot.

### 2. RTMP as a wire protocol

RTMP is a message-oriented protocol multiplexed over a single TCP connection (default port 1935). Primary sources: the Adobe RTMP spec (December 2012, now frozen), the invaluable **Thornburgh RTMP Errata and Addenda** (July 2024, https://zenomt.github.io/rtmp-errata-addenda/rtmp-errata-addenda.html) which fixes the spec's real ambiguities, and reference code in ffmpeg `rtmpproto.c`, nginx-rtmp-module, and Rust crates `rml_rtmp`/`xiu`.

**(a) The handshake.** Three packets each way, exchanged after TCP connect:
- **C0/S0**: 1 byte = version (0x03).
- **C1/S1**: 1536 bytes = `time`(4) + `zero`(4) + `random`(1528).
- **C2/S2**: 1536 bytes = echo of the peer's C1/S1 random.

That's the **simple** handshake. Flash Player 9+ and FMS introduced the **complex/digest** handshake (cleanly reverse-engineered in the CMU RTMPE document, https://www.cs.cmu.edu/~dst/Adobe/Gallery/RTMPE.txt). The `zero` field becomes a 4-byte server/client version; the 1528 bytes carry a 32-byte **HMAC-SHA256 digest** and a 128-byte Diffie-Hellman key at *obfuscated offsets*. The digest offset is computed by summing 4 bytes at a fixed location mod a window (the classic "scheme 0 vs scheme 1" ambiguity — you must try both and validate). The HMAC keys are fixed constants:
- `GENUINE_FP_KEY` = "Genuine Adobe Flash Player 001" + 32 binary bytes (`0xF0 0xEE 0xC2 0x4A …`), **30 bytes** used for the client digest.
- `GENUINE_FMS_KEY` = "Genuine Adobe Flash Media Server 001" + the same 32 binary bytes, **36 bytes** used for the server digest.

The digest is HMAC over the 1536 bytes *excluding the 32 digest bytes themselves*. **Practical lesson:** if you're writing a server-side packager, implement the simple handshake first; most encoders (OBS, ffmpeg) negotiate down. Implement the complex handshake only if a client demands it.

**(b) The chunk stream.** RTMP messages (a whole video/audio frame, or a command) are fragmented into **chunks** so that a large video message can't starve audio/control. Each chunk begins with a **Basic Header** encoding `fmt` (2 bits) and chunk-stream ID (csid):

```
1-byte form (csid 2-63):     [fmt:2][csid:6]
2-byte form (csid 64-319):   [fmt:2][000000] [csid-64:8]
3-byte form (csid 64-65599): [fmt:2][000001] [csid-64:16 little-endian]
```

Note the Errata clarification: the 3-byte csid is **little-endian** (easy to miss). Then the **Message Header**, whose length depends on `fmt`:

| fmt | size | fields | when used |
|-----|------|--------|-----------|
| 0 | 11 B | timestamp(3) + msg_len(3) + msg_type(1) + **msg_stream_id(4, little-endian)** | start of stream, or timestamp goes backward |
| 1 | 7 B | timestamp_delta(3) + msg_len(3) + msg_type(1) | new message, same stream (video: size varies) |
| 2 | 3 B | timestamp_delta(3) | new message, same stream & size (audio, cadence) |
| 3 | 0 B | (none) | continuation chunk, OR next message inheriting all fields |

Timestamps are **delta-compressed**: fmt 1/2 send a delta from the previous message on that csid; fmt 3 inherits the previous delta. This is why fmt 3 does double duty — both "more chunks of the current message" and "next message, same everything."

**The notorious fmt-3 extended-timestamp ambiguity.** When a timestamp (or delta) ≥ 0x00FFFFFF, the 3-byte field is set to 0xFFFFFF and a 4-byte **Extended Timestamp** is appended. The trap: **when the timestamp was extended, every subsequent fmt-3 chunk of that message MUST also carry the 4-byte extended timestamp**, even though fmt 3 has no timestamp field of its own. Implementations disagreed for years; the Thornburgh Errata §5.1 codifies the correct rule (presence of the extended timestamp is governed by whether the most recent type-0/1/2 chunk indicated it). Get this wrong and you either consume 4 bytes of payload as timestamp or vice-versa — instant corruption on long-running or high-timestamp streams.

**Message reassembly:** maintain per-csid state (current message type, length, timestamp, bytes-accumulated). Read chunk headers, append up to `min(remaining, chunkSize)` payload bytes, and dispatch the message when accumulated == msg_len.

**Protocol control messages** (on csid 2, msg-stream 0): Set Chunk Size (type 1), Abort (2), Acknowledgement (3), Window Acknowledgement Size (5), Set Peer Bandwidth (6); and **User Control Messages** (type 4: StreamBegin, SetBufferLength, etc.). The window-ack mechanism is RTMP's flow control: after receiving `window` bytes, the peer must send an Acknowledgement (type 3) or the sender may stall.

**(c) AMF0 command encoding.** Commands are AMF-serialized (type 20 = AMF0, type 17 = AMF3) messages. AMF0 is a tagged serialization; the type markers you actually need:

```
0x00 number (IEEE-754 double, 8 bytes big-endian)
0x01 boolean (1 byte)
0x02 string (UInt16 length + UTF-8)
0x03 object (key/value pairs; keys are bare UInt16-len strings; terminated by 0x00 0x00 0x09)
0x05 null
0x08 ECMA array (UInt32 count + members)
0x09 object-end marker
0x0A strict array
```

A command message = `commandName`(string) + `transactionId`(number) + `commandObject`(object or null) + args. **Transaction IDs pair requests with responses**: the client sends `connect` with txid 1, the server replies `_result` with txid 1. The publish flow (client → server):

```
connect(txid=1, {app, tcUrl, ...})     →   _result(txid=1)  [+ Window Ack Size, Set Peer BW server→client]
releaseStream(txid=2, streamName)       →   (optional, FMS-ism)
FCPublish(txid=3, streamName)           →   (optional, FMS-ism; onFCPublish)
createStream(txid=4)                    →   _result(txid=4, streamId)
publish(txid=5, streamName, "live")     →   onStatus(code="NetStream.Publish.Start")
@setDataFrame → onMetaData (data msg, type 18)
[video sequence header: AVCDecoderConfigurationRecord]
[audio sequence header: AudioSpecificConfig]
[interleaved video/audio media messages...]
```

`onStatus` carries an info object with a `code` string (`NetStream.Publish.Start`, `NetStream.Publish.BadName`, etc.); its transaction ID is 0 (no response expected). `@setDataFrame` wraps `onMetaData` (width, height, framerate, videocodecid, audiocodecid, bitrates).

**(d) FLV tag framing over RTMP.** RTMP media messages carry FLV tag *bodies* (the tag header's type/size/timestamp are redundant with the RTMP message header). Byte-level layouts (Adobe FLV spec §E.4, reproduced in the Enhanced RTMP v2 spec):

*Video tag (AVC), byte layout:*
```
Byte 0:  [FrameType:4][CodecID:4]   FrameType 1=keyframe/IDR, 2=inter; CodecID 7=AVC
Byte 1:  AVCPacketType (UI8)        0=sequence header (AVCDecoderConfigurationRecord)
                                    1=NALU, 2=end of sequence
Bytes 2-4: CompositionTime (SI24, signed, ms)   = PTS - DTS when type==1, else 0
Bytes 5+:  AVCDecoderConfigurationRecord (type 0) | length-prefixed NALUs (type 1)
```
The RTMP/FLV timestamp is the **DTS**; **PTS = DTS + CompositionTime**. CompositionTime is signed precisely so B-frame reordering (PTS < DTS) is representable.

*AVCDecoderConfigurationRecord (`avcC`, ISO/IEC 14496-15) — the sequence header body:*
```
[0] configurationVersion = 1
[1] AVCProfileIndication  (profile_idc, e.g. 0x42=Baseline, 0x64=High)
[2] profile_compatibility (constraint flags)
[3] AVCLevelIndication    (level_idc, e.g. 0x1F=3.1)
[4] 111111b | lengthSizeMinusOne(2)   0xFF ⇒ 4-byte NAL lengths (the field you MUST read)
[5] 111b | numOfSequenceParameterSets(5)   0xE1 ⇒ 1 SPS
[6-7] SPS length (UI16), then SPS NAL bytes
[..] numOfPictureParameterSets (UI8), then PPS length(UI16)+PPS bytes
```
Real example: `01 42 c0 1e ff e1 00 16 <22-byte SPS> 01 00 05 <5-byte PPS>` (Baseline profile 0x42, level 0x1E=30, 4-byte NAL lengths, 1 SPS of 22 bytes, 1 PPS of 5 bytes). The three fields you actually consume: **profile/level** (for the HLS `CODECS` attribute and the `avc1.PPCCLL` string), and **lengthSizeMinusOne** (almost always 3 → 4-byte length prefixes; you need it to walk NALUs). High/High10/etc. profiles append chroma_format and bit-depth fields in later 14496-15 editions.

*Audio tag (AAC), byte layout:*
```
Byte 0:  [SoundFormat:4][SoundRate:2][SoundSize:1][SoundType:1]
         SoundFormat 10=AAC; for AAC SoundRate SHOULD=3, actual params from ASC → byte is typically 0xAF
Byte 1:  AACPacketType (UI8)   0=AAC sequence header (AudioSpecificConfig), 1=raw AAC
Bytes 2+: AudioSpecificConfig (type 0) | raw AAC frame (type 1)
```

*AudioSpecificConfig (ISO/IEC 14496-3 §1.6.2.1), bit-packed MSB-first:*
```
audioObjectType        5 bits   (2=AAC-LC, 5=SBR/HE-AAC, 29=PS; 31=escape→+6-bit ext)
samplingFrequencyIndex 4 bits   (3=48000, 4=44100, 15=escape→24-bit explicit rate)
channelConfiguration   4 bits   (1=mono, 2=stereo, 6=5.1, 7=7.1)
```
Sampling-frequency-index table: 0=96000, 1=88200, 2=64000, 3=48000, 4=44100, 5=32000, 6=24000, 7=22050, 8=16000, 9=12000, 10=11025, 11=8000, 12=7350. Example: AAC-LC/44100/stereo = `0x12 0x10`; 48 kHz stereo = `0x11 0x90`. This 2-byte blob is the AAC sequence header and becomes the `esds` decoder-specific info in the audio init segment.

**(e) Enhanced RTMP (v1/v2).** The Veovera Software Organization revived RTMP for modern codecs (spec: https://veovera.org/docs/enhanced/enhanced-rtmp-v2.html). The mechanism: the video tag's first byte reuses the top bit of the FrameType nibble as an **IsExHeader** flag. If `(byte0 & 0x80)` is set, the low nibble is no longer CodecID but a **VideoPacketType**, followed by a 4-byte **FourCC** codec ID (the top bit was always 0 in pre-2023 streams, so setting it unambiguously signals the new format):

```
VideoPacketType nibble: 0=SequenceStart, 1=CodedFrames (SI24 CTS follows),
   2=SequenceEnd, 3=CodedFramesX (CTS omitted, assumed 0),
   4=Metadata (AMF, e.g. HDR colorInfo), 5=MPEG2TSSequenceStart; v2 adds 6=Multitrack, 7=ModEx
FourCC: 'avc1' (H.264), 'hvc1' (HEVC), 'av01' (AV1), 'vp09' (VP9), 'vvc1' (VVC/v2)
```
`CodedFramesX` is a nice micro-optimization — it drops the 3-byte composition-time offset when it's zero (typical for low-latency, no-B-frame streams). Enhanced RTMP also adds multitrack, a Reconnect Request, nanosecond timestamp offsets, and enhanced audio (SoundFormat 9 escape → AudioPacketType + audio FourCC: `mp4a`, `Opus`, `fLaC`, `ac-3`, `ec-3`). Softvelum's Nimble/Larix and SRS/mediamtx already support it for HEVC/AV1.

**Where RTMP shows its age:** single TCP connection → **head-of-line blocking** (one lost packet stalls everything, no partial reliability); **32-bit millisecond timestamps** → rollover at ~49.7 days but, more relevantly, the extended-timestamp complexity above; **no FEC, no native reconnect/resume** (a dropped TCP connection means restarting the publish handshake — Enhanced RTMP's Reconnect Request patches this); and **single-track assumptions** baked into the message model (Enhanced RTMP multitrack is a retrofit).

### 3. Ingest at scale — how real platforms accept contribution feeds

**Auth and admission.** nginx-rtmp exposes `on_connect`/`on_publish` HTTP callbacks: on `publish`, nginx fires an async HTTP request with the stream name and args; a 2xx admits the stream, non-2xx rejects it (3xx redirects). This is where stream-key validation lives. Managed platforms (Mux, Cloudflare Stream, AWS IVS, YouTube, Twitch) do the same conceptually — a stream key in the RTMP URL/path is validated against the account before media is accepted.

**Documented ingest limits (publicly stated):**
- **YouTube Live**: RTMP/RTMPS, H.264 + AAC-LC (128 kbps stereo / 384 kbps 5.1), **CBR**, keyframe interval **2 s recommended, 4 s max**; recommended bitrates ~4.5-6 Mbps for 1080p30, up to ~35 Mbps for 2160p60 (HEVC/AV1 via Enhanced RTMP accepted 10-40 Mbps). YouTube uses GOP boundaries as HLS split points — "auto"/scene-cut keyframes misalign segments and trigger the "stream not stable" warning.
- **Twitch**: non-partner cap **6000 kbps**, CBR, 2 s keyframe, 2 B-frames; AV1 ingest via WHIP rolling out (enhanced broadcasting: 1080p60 @ 8 Mbps AV1).
- **Kick**: 1000-8000 kbps, CBR (VBR *not* supported), 2 s keyframe, ≤1080p60.

The near-universal constant: **2-second keyframe interval, CBR**. Both exist to serve the downstream packager — GOP boundaries become segment boundaries, and CBR keeps segment sizes predictable for ABR.

**Fast join / GOP buffering.** A viewer can only start decoding at an IDR. Ingest servers keep a **GOP cache** (the last keyframe + following frames) so a new subscriber gets a decodable frame immediately instead of waiting up to one GOP. nginx-http-flv-module adds `gop_cache`; SRS makes it tunable (and notes it *increases* latency — a fast-join vs latency tradeoff). nginx-rtmp's `wait_key on`/`wait_video on` force streams to begin on a keyframe.

**Backpressure.** If the publisher outruns the wall clock (encoder bursts), the packager's bounded queue fills; the correct behavior is to bound the in-memory window and drop the oldest un-referenced data, never to grow unbounded. If the publisher *lags* (network stall), the packager must either stall its own output (raising latency) or emit a discontinuity. **A/V clock drift**: audio and video arrive with independent timestamp clocks; over hours they drift. The packager must trust the RTMP timestamps as the master timeline and resample/round consistently rather than assume fixed frame durations.

### 4. Demux → CMAF fMP4 remux (bitstream passthrough, timeline, keyframes)

The goal: take H.264 + AAC out of FLV and rewrap into a CMAF init segment + fMP4 fragments **without touching the compressed samples**.

**AVCC vs Annex-B.** Two ways to delimit NALUs: **Annex-B** uses start codes (`0x000001`/`0x00000001`) and is used in MPEG-TS/live elementary streams; **AVCC** (a.k.a. length-prefixed / `avc1`) prefixes each NALU with a length field and stores SPS/PPS separately in `avcC`. **FLV carries AVCC already**, and fMP4 wants AVCC — so for the RTMP→fMP4 path **no start-code conversion is needed**; you copy the length-prefixed NALUs straight into `mdat`. (You'd only convert to Annex-B if targeting TS output.) Watch the `lengthSizeMinusOne` value from `avcC` in case a producer uses 2-byte lengths.

**Timeline math (the only real computation).**
- Choose a **timescale** (video: 90000 is conventional and divides common frame rates cleanly, or use the sample rate for audio, e.g. 48000). Convert FLV ms timestamps into timescale units.
- **DTS** = FLV timestamp; **PTS** = DTS + CompositionTime. Per sample, `composition_time_offset = PTS - DTS`.
- **`baseMediaDecodeTime`** (in `tfdt`) = the DTS of the first sample of the fragment, in timescale units. This builds a continuous media timeline across fragments. **Beware rounding drift**: if you convert each ms→timescale independently and round, errors accumulate over hours; carry a running accumulator in the source timescale.
- **Cut fragments on IDR** (FrameType==1). A CMAF *segment* must start with an IDR (a Stream Access Point); a CMAF *chunk*/part need not, but the first chunk of a segment must.

**B-frames → signed CTS → trun version 1.** If CompositionTime is ever nonzero/negative, you must use `trun` **version 1** whose `sample_composition_time_offset` is **signed 32-bit**. Version 0 is unsigned and cannot represent PTS < DTS.

**Init segment box layout** (build once, from the sequence headers):
```
ftyp  major_brand=cmf2/iso6, compatible: iso6,cmfc,...
moov
 ├─ mvhd  (version 1; timescale, duration=0 for live)
 ├─ trak
 │   ├─ tkhd (version 1, flags=7 enabled/in-movie; track_ID, width/height in 16.16)
 │   └─ mdia
 │       ├─ mdhd (timescale, duration=0)
 │       ├─ hdlr ('vide' or 'soun')
 │       └─ minf
 │           ├─ vmhd/smhd
 │           ├─ dinf/dref (self-contained: url flag=1)
 │           └─ stbl
 │               ├─ stsd → avc1 → avcC   (the AVCDecoderConfigurationRecord)
 │               │         (audio: mp4a → esds with AudioSpecificConfig)
 │               └─ stts/stsc/stsz/stco  (all empty for fMP4)
 └─ mvex
     └─ trex  (track_ID, default_sample_description_index=1, default durations/flags)
```
`mvex`/`trex` is the signal "this is fragmented — expect `moof`s." Without it, parsers treat `moov` as complete and ignore fragments.

**Media fragment box layout** (per part/chunk):
```
[styp]  (segment type; optional per chunk, present at segment start)
moof
 ├─ mfhd  sequence_number (monotonic, per fragment)
 └─ traf
     ├─ tfhd  track_ID; flags select defaults (default-base-is-moof 0x020000 is standard for CMAF)
     ├─ tfdt  version 1 → baseMediaDecodeTime (64-bit)
     └─ trun  version 1; flags: data-offset-present(0x01),
              first-sample-flags-present(0x04),
              sample-duration(0x100), sample-size(0x200),
              sample-flags(0x400), sample-composition-time-offset(0x800)
mdat  (the AVCC NALUs / AAC frames, verbatim)
```

**The `data_offset` back-patch.** `trun.data_offset` is measured **from the first byte of the `moof`** to the first sample in `mdat`. You cannot know it until the entire `moof` (including the `trun` itself, whose size depends on sample count) is serialized. The idiom: write `trun` with `data_offset=0` placeholder, finish the `moof`, compute `offset = moof_size + 8` (the `mdat` header), then seek back and patch the 4 bytes. With `default-base-is-moof` set and no `base_data_offset`, all addressing is `moof`-relative, which is what CMAF requires.

**tfhd/trun flags that bite:** if you set `default_sample_flags` in `tfhd` you must *not* also set per-sample flags unless intended; the first sample of a segment needs its own flags (`first-sample-flags-present`) marking it a **non-dependent sample** (IDR, `sample_depends_on=2`, `sample_is_non_sync_sample=0`) while following samples set `sample_is_non_sync_sample=1`. Players use these flags to find seek points.

**CMAF chunks/parts.** A CMAF *chunk* is one `moof`+`mdat` pair covering ~100-400 ms. A *segment* is a sequence of chunks starting at an IDR. For LL-HLS each chunk = one `EXT-X-PART`; for LL-DASH the chunks stream inside one segment's HTTP response via chunked transfer.

### 5. Low-latency delivery — LL-HLS blocking reload, parts, vs LL-DASH

**Why plain HLS is slow.** RFC 8216 live HLS: the server publishes N-second segments; the client polls the playlist on a timer and must buffer ~3 target durations from the edge (the spec forbids removing a segment if the playlist would drop below 3× target duration — this is the "3-segment rule," designed so a client that refreshes slightly late still has media). With 6 s segments that's ~18-30 s.

**LL-HLS mechanisms** (Apple HLS Authoring Spec, current rev 2025-09; draft-pantos-hls-rfc8216bis §6.2.5.2):

1. **`EXT-X-PART`** — a partial segment, ~200 ms, addressable before its parent segment completes. `INDEPENDENT=YES` marks a part that starts with an IDR (the only place a client may switch renditions or begin decoding).
2. **`EXT-X-PART-INF:PART-TARGET=`** — the maximum part duration (drives client request cadence).
3. **`EXT-X-SERVER-CONTROL`** — `CAN-BLOCK-RELOAD=YES` (mandatory for LL), `PART-HOLD-BACK` (minimum distance from live edge a client may play; **must be ≥ 3× PART-TARGET**), `HOLD-BACK` (for legacy clients), `CAN-SKIP-UNTIL` (enables delta playlists).
4. **`EXT-X-PRELOAD-HINT:TYPE=PART`** — announces the URI of the next part *before it exists* so the client's request is already in flight; the server holds it open until the bytes exist. (`TYPE=MAP` preloads the next init segment at ABR boundaries.)
5. **`EXT-X-RENDITION-REPORT:URI=…,LAST-MSN=…,LAST-PART=…`** — tells the client the live position of *other* renditions so it can switch without a probe round-trip.
6. **`EXT-X-SKIP` + `CAN-SKIP-UNTIL`** — playlist delta updates: the client sends `_HLS_skip=YES` and the server elides old segments, shrinking the playlist.

**Blocking playlist reload — exact semantics.** The client requests `playlist.m3u8?_HLS_msn=M&_HLS_part=N`:
- If a playlist that already contains part N of media-sequence M exists → return it immediately.
- If not → **hold the request open** until that part is produced, then return.
- The server **SHOULD return 400** if the client asks for a Media Sequence Number in the past (already removed), or asks too far in the future — the spec bounds the request to roughly the next part or the one after; asking further ahead is an error.
- Requests that would block longer than **3× target duration** should not be honored (return what's available / a Rendition Report).

An example LL-HLS media playlist:
```
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-TARGETDURATION:1
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.6,CAN-SKIP-UNTIL=6.0
#EXT-X-PART-INF:PART-TARGET=0.200
#EXT-X-MEDIA-SEQUENCE:1045
#EXT-X-MAP:URI="init.mp4"
#EXTINF:1.000,
1044.m4s
#EXT-X-PART:DURATION=0.200,URI="1045.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.200,URI="1045.1.m4s"
#EXT-X-PART:DURATION=0.200,URI="1045.2.m4s"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="1045.3.m4s"
#EXT-X-RENDITION-REPORT:URI="../720p/playlist.m3u8",LAST-MSN=1045,LAST-PART=2
```

**Parts as byte-ranges vs separate files.** A part can be a standalone file (`URI="1045.0.m4s"`) or a byte-range into the growing segment file (`EXT-X-PART:...,BYTERANGE="20000@0"`). Byte-range mode plays nicer with CDNs (fewer objects, and the completed segment is one cacheable file) and mirrors LL-DASH's open-ended range requests; separate files are simpler to produce. Apple's authoring rules: expose one independent part ≥ every ~1 s of real time; keep PART-TARGET consistent; the playlist must still satisfy the 3× target-duration retention rule.

**LL-DASH** (DASH-IF Low-Latency IOP, current rev 2024; https://dashif.org/docs/CR-Low-Latency-Live-r8.pdf). Same CMAF chunks, different delivery: instead of listing each chunk in the manifest, the MPD declares `SegmentTemplate` with `availabilityTimeOffset` (how many seconds before nominal completion the segment becomes fetchable) and `availabilityTimeComplete="false"` (segments are produced progressively). The client opens a request for the in-progress segment and the origin **streams CMAF chunks over HTTP chunked transfer encoding**, frame-group by frame-group, never closing until the segment finishes. `ProducerReferenceTime` (`prft`) boxes + a `UTCTiming` element let the client measure true latency and compensate for clock drift. Recommended chunk duration ≤1 s; consensus 200-400 ms chunks with 1-2 s segments → 2-4 s glass-to-glass.

**LL-HLS vs LL-DASH, honestly.** They are two front-ends over the *same* CMAF bytes. LL-HLS puts intelligence in the *playlist* (parts enumerated, long-poll discovery); LL-DASH puts it in the *transport* (chunked transfer, manifest declares timing math). The 2026 production stack is one set of CMAF chunks served as LL-HLS to Apple devices and LL-DASH to everything else, behind one origin. Convergence since Apple removed HTTP/2 push (Sept 2023) and allowed byte-range/open-ended part requests is real — an LL-HLS client can issue open-ended range requests like LL-DASH.

**Real glass-to-glass, by approach:** plain HLS/DASH 15-30 s; LL-HLS 2-5 s (tunable to ~1.5 s with 350 ms parts per WINK's 2025 experiments); LL-DASH 2-4 s; **SRT contribution** adds a tunable buffer of a few × RTT to whatever the downstream delivery costs; **WebRTC/WHIP** 0.2-0.5 s.

### 6. Fan-out, CDN, and edge request coalescing

**Mux once, serve N.** The packager produces one set of parts/segments; the CDN fans out. The scaling problem unique to LL-HLS is **request amplification**: with 200 ms parts, each viewer generates ~5 part requests/sec **plus** a blocking playlist reload per part — so `~10 req/s × viewers`, all of them long-held connections.

**Held requests cost connections, not CPU.** A blocking reload or preload-hint request sits open for up to a part duration. At the edge this is cheap per-connection but multiplies: 100k viewers × held requests = 100k+ open sockets per POP. **This is why HTTP/2 and HTTP/3 matter** — they multiplex many held streams over one connection, avoiding socket exhaustion. (Apple originally *required* HTTP/2; the requirement was dropped but the multiplexing benefit remains.)

**Request coalescing / origin shield.** When a part isn't cached and 10,000 edge requests arrive simultaneously, the CDN must **collapse them into a single origin fetch** (request coalescing) and fan the response back out; an **origin shield** (a mid-tier cache all POPs fill through) protects the packager during live peaks. Google Media CDN, for example, describes three tiers (deep edge → peering edge → long-tail caches acting as origin shield). Without coalescing, a live spike becomes a DDoS on your origin.

**Cache keys MUST include `_HLS_msn`/`_HLS_part`.** Both AWS MediaPackage and Akamai document this explicitly: the CDN cache key must incorporate the `_HLS_*` query params (Akamai's advice: add `_HLS` with Exact-Match=No to catch them all), or every distinct blocking request collapses to the same cache entry and clients get stale playlists. Conversely, the CDN must **not** override the origin's `Cache-Control`. Parts and completed segments get long TTLs (they're immutable once produced — MediaPackage recommends up to 14 days for CMAF segments); the live *playlist* is effectively uncacheable / very-short-TTL and relies on the held-request model rather than polling into 404s. The historic failure mode LL-HLS was designed around: clients polling for a not-yet-existing segment and hammering the origin with 404s. Blocking reload replaces the 404 storm with held connections.

**CDN adaptation.** Akamai (Adaptive Media Delivery), Fastly, Cloudflare, and CloudFront/MediaPackage all added: held-request support (don't time out a blocked origin response too early), request coalescing, `_HLS_*` cache-key handling, and HTTP/2+ at the edge. A misconfigured CDN that caches each part as a separate un-coalesced entry, or times out held requests, silently degrades LL-HLS into polling HLS — the single most common production failure.

### 7. Reliability, correctness, and how to test a packager

**Bounded live window.** Hold a rolling window in memory: enough complete segments to satisfy the 3× target-duration retention rule plus the parts of the current segment, and a GOP buffer for fast join. Evict oldest segments as new ones complete; free their parts. Retaining too little violates the spec (stalls); too much wastes memory and lets clients drift far from live.

**Discontinuities.** `EXT-X-DISCONTINUITY` signals a break in the media timeline (codec/resolution/timescale change, encoder restart, timestamp jump). `EXT-X-DISCONTINUITY-SEQUENCE` (must appear before the first segment) tracks how many discontinuities preceded the current window so clients keep a stable timeline across playlist reloads — a mismatch between reloads is a critical `mediastreamvalidator` error and stalls Safari. Use `EXT-X-GAP` for a missing segment (better than letting the client 404). **RTMP 32-bit ms timestamp rollover**: at ~49.7 days it wraps; more practically, an encoder restart resets timestamps to near zero. Detect a large backward jump and emit a discontinuity + reset `baseMediaDecodeTime` rather than producing negative deltas.

**How to test a from-scratch packager:**
- **HLS conformance**: Apple's `mediastreamvalidator` + `hlsreport` (simulates a session, checks playlist syntax, segment continuity, IDR intervals, the 3× rule, discontinuity consistency). This is the canonical gate.
- **fMP4/box validation**: `MP4Box -info`, Bento4 `mp4dump`/`mp4info`, `ffprobe`. In Rust, `mp4box`/`mp4-rust` decode the box tree to structs and validate stco/co64, `tfdt`, `trun` sample tables.
- **DASH**: DASH-IF conformance tooling for MPD + segment validation.
- **Deterministic tests for your own code**:
  - **Golden files**: capture a known RTMP publish, assert byte-exact init segments and `moof` structures (patch out timestamps).
  - **Fuzz the chunk reader**: feed truncated/garbage chunk headers; it must never panic or read out of bounds (the Thornburgh errata's "abort on truncation" rule).
  - **Property tests on timestamp math**: for random DTS/CTS sequences, assert PTS monotonic where expected, `baseMediaDecodeTime` continuity across fragments, and no drift accumulation (sum of durations == last DTS − first DTS).
  - **Round-trip**: remux → `ffprobe`/`mediastreamvalidator` → assert the reported profile/level/fps/sample-rate match the source `avcC`/ASC.

### 8. Advanced & emerging topics (2025-2026)

**SRT and RIST (contribution successors to RTMP).** Both are UDP-based with **ARQ** (receiver NACKs missing packets, sender retransmits) and a tunable latency buffer that trades reliability for delay. The **latency = multiplier × RTT** rule: at low loss use ~3× RTT; higher loss needs a bigger multiplier (Epiphan/practitioner tables map loss % → multiplier). SRT (Haivision, 2017, built on UDT) uses **NACK + ACK**, point-to-point, and **caller / listener / rendezvous** connection modes (caller-to-listener is standard; rendezvous only for symmetric-NAT-both-sides; listener for a central ingest accepting many callers). AES-128/256 is essentially free on modern hardware — enable it always. **SRTLA** bonds multiple cellular uplinks for IRL/mobile. RIST uses **NACK-only** ARQ, adds point-to-multipoint/multicast, and bypasses firewalls via RTCP. Both are in AWS MediaConnect, ffmpeg, GStreamer, VLC via `libsrt`/`librist`. **Deployment reality (2026):** SRT is the professional public-internet contribution standard — Haivision's 2024 Broadcast Transformation Report (800+ professionals) found "a resounding 68% of respondents are utilizing SRT for live video transport… RTMP is used by 56%… UDP by 45%." RIST is favored where broadcast equipment standardizes on it. SRT is *one leg* (contribution) — total latency still includes encode, package, and player buffer.

**WebRTC / WHIP (sub-second ingest).** **WHIP is a finished IETF standard — RFC 9725, "WebRTC-HTTP Ingestion Protocol," Standards Track, March 2025** (S. Garcia Murillo/Millicast & A. Gouaillard/CoSMo Software, from draft-ietf-wish-whip-16; updates RFCs 8840 and 8842). It standardizes only the *signaling*: the encoder sends an SDP offer via **HTTP POST**, gets a `201 Created` with the SDP answer, then media flows over standard WebRTC (ICE for NAT traversal, DTLS-SRTP encryption, RTP/RTCP transport); an HTTP `DELETE` tears down. It maps the RTMP mental model (URL + stream key) onto WebRTC's sub-second transport. OBS shipped native WHIP in v30; Cloudflare, AWS IVS Real-Time, Dolby, Red5, Ant Media expose WHIP endpoints. **When it beats SRT:** interactive/conversational latency (0.2-0.5 s), browser-native sources, live betting/auction/telemedicine. **When SRT still wins:** high-bitrate 4K contribution over lossy/satellite links — WebRTC's congestion control wasn't designed for multi-megabit fixed-rate paths; SRT's ARQ is more predictable. **The cost:** WebRTC packaging for scale needs an SFU (one publisher → forwarded to subscribers) + TURN relays + signaling; you also lose the cheap CDN economics of HTTP streaming, and reaching HLS clients means transcoding/repackaging server-side.

**WebTransport & Media over QUIC (MoQ).** IETF `moq-transport` is an active, fast-moving draft — **draft-ietf-moq-transport-18 "Media over QUIC Transport," published 12 May 2026 (expires 13 November 2026), authors S. Nandakumar (Cisco), V. Vasiliev & I. Swett (Google), A. Frindell (Meta)** — a **publish/subscribe protocol that runs over QUIC and WebTransport**, operating "both point-to-point and through intermediate relays, enabling scalable low-latency delivery." Its model: media is **Objects** grouped into **Groups** within **Tracks**, addressed by name; **Relays** (which are both publisher and subscriber) forward objects by track alias to matching subscribers, enabling CDN-scale sub-second fan-out — the "third option" between WebRTC's interactivity and HLS/DASH's reach. QUIC gives per-stream loss recovery (no head-of-line blocking) and partial reliability. **Honest status (2026):** interop was first demonstrated at IETF 118; nanocosmos claims first production deployment (2025, low-hundreds-of-thousands concurrent). The `moq-dev/moq` project ships `moq-lite`, a deployable forwards-compatible subset (works with Cloudflare's moq relay). Verdict: **feature-complete enough for demos and targeted production, but not yet boring default infrastructure** — features like REWIND for join behavior were still in flux in early 2026. `moq-rs` is the Rust reference implementation.

**AV1 and low-latency codecs.** AV1 delivers large bitrate savings at equal quality — NVIDIA's developer blog reports "NVENC AV1 encoding results in a 40% bit rate savings over H.264 at 1080p60" (AV1 reaching 42 dB PSNR at 7 Mbps where H.264 needs 11 Mbps), and at Netflix scale AV1 sessions in 2025 used roughly a third less bandwidth than AVC and HEVC for matched quality. For **live**, hardware encoders are effectively required: NVENC AV1 (RTX 40-series, 500+ fps @1080p), Intel Quick Sync AV1 (Arc, cheapest for transcode farms), AMD AMF AV1 (RDNA 3). **SVT-AV1** at real-time presets (M10-M13) can keep up on beefy multicore but at high CPU cost; a low-latency arXiv study (2511.18688) found SVT-AV1 presets 10/12 produced near-identical output across latency-tuning modes, while NVENC AV1 traded ~0.3 dB PSNR moving to low-latency tuning. **Twitch ships AV1 ingest via WHIP** (1080p60 @ 8 Mbps). **LCEVC** (MPEG-5 Part 2) is an enhancement layer that boosts a base codec's efficiency at low complexity — useful paired with SVT-AV1 to hit real-time. **Codec reality:** H.264 remains the universal baseline for contribution and delivery; HEVC/AV1 via Enhanced RTMP or WHIP are production-viable where hardware and decoder support align, but H.264 is still what you ship for compatibility.

**Adoption summary (2026):** LL-HLS + LL-DASH off shared CMAF is the production default for one-to-many low-latency at scale; WebRTC/WHIP owns sub-second and interactive; MoQ is the promising frontier. RTMP is legacy but not dead — it's still one of the most widely supported contribution on-ramps.

## Recommendations

**For a from-scratch Rust learner on a single node with an in-memory window, adopt (in order):**

1. **RTMP server-side ingest with the SIMPLE handshake, H.264 + AAC only.** Skip RTMPE/complex handshake and AMF3. Implement the chunk-stream reader carefully — this is where the learning is. Reference `rml_rtmp`/`xiu` for structure but write your own. **Nail the fmt-3 extended-timestamp rule and message reassembly**; fuzz the reader from day one.
2. **A strict publish state machine** (connect → createStream → publish → onStatus) with AMF0 only. Reject anything else. Validate transaction-ID pairing.
3. **AVCC→CMAF remux with bitstream passthrough.** No re-encode, no Annex-B conversion. Build the init segment once from the sequence headers; emit `moof`+`mdat` per fragment. **Get `tfdt` baseMediaDecodeTime, signed CTS (trun v1), and the data_offset back-patch right** — validate every output with `MP4Box -info` and `ffprobe`.
4. **LL-HLS with a single rendition, parts as separate files first** (byte-ranges later). Implement blocking reload (`_HLS_msn`/`_HLS_part` long-poll), `EXT-X-PART`, `EXT-X-PRELOAD-HINT`, `EXT-X-SERVER-CONTROL`. **Gate every change on `mediastreamvalidator`.** Serve over HTTP/1.1 chunked or HTTP/2 locally.
5. **A bounded rolling window** with GOP buffer and correct eviction (respect the 3× target-duration rule).

**Skip (they're production concerns, not learning primitives at single-node scale):** ABR ladders and rendition reports (start with one rendition), CDN/origin-shield/coalescing (irrelevant at N=few viewers — but *understand* the amplification math), DRM/CENC, Enhanced RTMP multitrack, LL-DASH (add as a second front-end only after LL-HLS works), WebRTC/WHIP and MoQ (different transport model — a separate project).

**Benchmarks that change the plan:** if you need <1 s glass-to-glass or interactivity, stop building HLS and adopt WHIP/WebRTC. If you need >few-thousand concurrent viewers, you now need a CDN with coalescing + origin shield and the cache-key discipline in §6. If contribution runs over the lossy public internet, put SRT (not RTMP) on the first leg. If you must serve non-Apple and Apple devices at low latency, produce CMAF once and add an LL-DASH manifest.

## Caveats
- **The RTMP spec (2012) is frozen and buggy.** Treat the Thornburgh Errata (2024) as the authoritative correction, especially for byte-order and the extended-timestamp rule. Behavior in the wild varies; test against OBS *and* ffmpeg.
- **"LL-HLS requires HTTP/2 push" is outdated.** Apple removed HTTP/2 server push in Sept 2023; preload hints are now the mechanism. Articles predating that are wrong — verify dates.
- **Exact blocking-reload error rules (400 vs 503, how-far-future) are subtle** and stated across the draft-pantos bis document and Apple's authoring spec; implementations differ. When in doubt, return what you have plus a Rendition Report rather than blocking indefinitely.
- **Latency numbers are workload-dependent.** The 2-5 s LL-HLS and 0.2-0.5 s WebRTC figures are practitioner medians; tails (p95/p99) blow out during ABR switches, cache misses, and packet loss — the tail is what viewers notice.
- **MoQ is a moving target.** Draft numbers and features (REWIND, relay semantics) changed through 2025-2026; anything you build against it will need maintenance. It is not yet stable infrastructure.
- **Enhanced RTMP is community-driven (Veovera), not Adobe.** Adoption is real (OBS, SRS, mediamtx, Nimble/Larix) but not universal; a receiver must negotiate codec support via the `connect` command's capability list and fall back gracefully.
- **AV1 live is hardware-gated.** Software SVT-AV1 real-time is possible but CPU-expensive; decoder support on older client devices is still uneven. H.264 remains the safe default.
