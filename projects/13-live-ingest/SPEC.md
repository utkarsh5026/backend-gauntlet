<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 13 — Live Ingest Server (RTMP → LL-HLS)

> Project 11 packaged a file that already existed; project 12 produced that file
> from an upload. This one has no file — the media is *arriving*, right now, from a
> camera. A broadcaster (OBS, `ffmpeg`, a phone) opens a socket and starts pushing
> H.264 + AAC over **RTMP**, and thousands of viewers want to watch **within a few
> seconds of real life**. That "few seconds" is the whole game: regular HLS cuts
> 6-second segments and makes a player buffer three of them, so the viewer is ~15–30
> seconds behind the glass. **Low-Latency HLS** breaks that by publishing each
> segment as it forms — in ~200 ms **parts** — and letting the player *block* on the
> playlist until the next part exists, so it never polls into a 404 and never waits a
> whole segment. None of the hard parts are a library call. RTMP is a binary protocol
> over raw TCP: a three-message **handshake**, then a **chunk stream** that shreds
> every message into ≤128-byte chunks with a stateful, delta-compressed header you
> must reassemble, carrying **AMF0**-encoded commands (`connect`, `publish`) you parse
> and answer. Then you take those live H.264/AAC frames and, *without re-encoding*,
> rewrap them into **CMAF fMP4** fragments on a running timeline, cut LL-HLS parts and
> segments on keyframes, and serve a playlist that changes every 200 ms to players
> that are holding their requests open waiting for it. It's a `<video>` tag pointed at
> a socket that a person is speaking into — turned into a demux, a mux, and a
> latency-critical delivery protocol you build yourself.

## What it does (the easy part)
- Listens for **RTMP** on `RTMP_PORT` (default `1935`). A broadcaster publishes to
  `rtmp://host:1935/live/<stream-key>` — the stream key both names the stream and
  authorizes the publish.
- Repackages the live audio/video into **LL-HLS** and serves it over HTTP on
  `HTTP_PORT` (default `8080`):
  - `GET /live/{key}/index.m3u8` — the live media playlist (supports the LL-HLS
    **blocking reload** query params `_HLS_msn` / `_HLS_part`).
  - `GET /live/{key}/init.mp4` — the CMAF init segment.
  - `GET /live/{key}/seg/{msn}.m4s` — a full media segment.
  - `GET /live/{key}/part/{msn}/{part}.m4s` — one partial segment (a **part**).
- `GET /live` lists the streams currently on air; `GET /healthz` is liveness.

> There is **no database and no docker-compose** here: the source is a live socket,
> and everything downstream lives in a bounded in-memory window (a live stream is
> unbounded — you keep only the last few seconds). The parts you'd normally hand to
> `nginx-rtmp` / `ffmpeg` / a packager — the RTMP handshake and chunk-stream reader,
> the AMF command handling, the fMP4 repackaging, the LL-HLS playlist and its blocking
> reload — are exactly the parts you build. To exercise it you need an RTMP source
> (`ffmpeg -re -i in.mp4 -c copy -f flv rtmp://localhost:1935/live/testkey`, or OBS)
> and an LL-HLS player (Safari natively, or `hls.js` with low-latency mode).

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. RTMP handshake + chunk-stream reader — *parse the wire by hand*
In `src/rtmp.rs`, accept a raw TCP connection and turn the RTMP byte stream into a
sequence of complete **messages**. This is the protocol floor everything else stands
on, and it is pure binary parsing over a socket — no library.

RTMP opens with a **handshake**: the client sends `C0` (1 version byte) + `C1` (1536
bytes: a timestamp, a zero field, and 1528 random bytes); the server answers `S0` +
`S1` + `S2`, and the client sends `C2` — each side echoing the other's random block.
Only then does data flow, and it flows as a **chunk stream**: every message is split
into chunks of at most a negotiated size (default **128 bytes**), each prefixed by a
**basic header** (a 2-bit format + a chunk stream id that itself is 1, 2, or 3 bytes
wide) and — for format 0/1/2 — a **message header** that is *delta-compressed* against
the previous chunk on that same stream id (type 3 repeats everything). Timestamps ≥
`0xFFFFFF` spill into an **extended timestamp** field. Reassembling those chunks back
into whole messages — while honoring a mid-stream **Set Chunk Size** — is the work.

**Done when ALL true:**
- [ ] The **handshake completes** with a real broadcaster (`ffmpeg`/OBS): after
  C0/C1↔S0/S1/S2↔C2 the peer proceeds to send commands — a wrong echo or length and it
  hangs up, so completion *is* the proof it's byte-correct.
- [ ] A message split across **multiple chunks** is reassembled into one message with
  the correct length and payload — no chunk boundary ever leaks into the message body.
- [ ] All four chunk **header formats (0–3)** decode, with fmt 1/2/3 correctly
  inheriting the missing fields (timestamp delta, message length, type id, stream id)
  from the prior chunk on that chunk stream id.
- [ ] **Extended timestamps** (value `0xFFFFFF`) are read from the 4 extra bytes, and a
  mid-stream **Set Chunk Size** changes the reassembly boundary from then on.
- [ ] A **truncated, oversized, or malformed** chunk is rejected as an error that ends
  the session cleanly — never a panic, an unbounded allocation, or an out-of-bounds read.

**Proof:** unit tests decoding captured chunk sequences (fmt 0→3 inheritance, a
multi-chunk message, an extended timestamp) into expected messages
(`reassembles_multichunk_message`, `chunk_header_fmt_inheritance`); a fuzz/property test
that random/truncated bytes never panic the reader (`malformed_chunks_never_panic`); and
a live handshake with `ffmpeg` reaching the command phase, noted in `docs/13-design.md`.

*Concept to internalize:* why a media protocol multiplexes messages into small chunks
(head-of-line blocking on a shared TCP connection), how RTMP's delta-compressed chunk
headers save bytes on a steady stream, and why the handshake's random echo exists.

### V2. AMF0 commands + the publish state machine — *speak RTMP's control language*
In `src/amf.rs` (the AMF0 codec) driven by `src/session.rs` (the state machine), decode
the **command messages** RTMP carries and answer them, walking a connection from
"just handshook" to "publishing live media". The chunk reader (V1) hands you message
bodies; the control ones are AMF0-encoded RPC.

**AMF0** is a compact typed serialization: a 1-byte type marker then the value —
`number` (f64 BE), `boolean`, `string` (u16-length-prefixed), `object` (key/value
pairs terminated by an empty-key `object-end`), `null`. A publisher's opening
sequence is `connect(app)` → *(reply `_result`)* → `releaseStream`/`FCPublish` →
`createStream` → *(reply `_result` with a stream id)* → `publish(key, "live")` →
*(reply `onStatus` `NetStream.Publish.Start`)* — after which every message on that
stream is audio, video, or metadata. You also parse the **codec configuration** out of
the first video/audio messages: the AVC **sequence header** (SPS/PPS, i.e. the
`avcC`) and the AAC **AudioSpecificConfig** — the setup V3 needs to build an init
segment.

**Done when ALL true:**
- [ ] AMF0 **decodes and encodes** the value types a publish flow uses (number,
  boolean, string, object, null) and **round-trips** (decode∘encode is identity on
  those); a value with a trailing/short buffer errors, never panics.
- [ ] The session drives the **full publish sequence**: it answers `connect` with
  `_result`, `createStream` with a stream id, and `publish` with an `onStatus`
  `NetStream.Publish.Start` — a real broadcaster transitions to sending media.
- [ ] The session is a **state machine**: media (audio/video) messages are accepted
  **only after** a successful `publish`, and an out-of-order or duplicate command is
  handled without corrupting state (rejected or ignored, documented which).
- [ ] The **codec config is extracted**: the AVC sequence header (SPS/PPS → `avcC`,
  with width/height) and the AAC AudioSpecificConfig are captured from the first tags
  and handed to the packager — not the per-frame data, the *setup*.
- [ ] A publish to an **unknown/absent stream key is refused** (see security) and the
  session closes — an open ingest is a takeover vector, so the key gates the transition
  to the publishing state.

**Proof:** unit tests round-tripping AMF0 values and decoding a captured `connect`
/`publish` command (`amf0_roundtrips_publish_command`); a state-machine test that a
media message before `publish` is rejected and after `publish` is accepted
(`media_rejected_before_publish`); a live `ffmpeg` publish reaching the media phase,
noted in `docs/13-design.md`.

*Concept to internalize:* AMF0's typed-marker wire format; RTMP's command/response
RPC and the `connect`→`createStream`→`publish` sequence; and why the ingest is a state
machine with an auth gate, not a blind byte pump.

### V3. Live fMP4 repackaging — *rewrap H.264/AAC into CMAF, no re-encode*
In `src/fmp4.rs`, turn the live stream of AVC access units + AAC frames (from V2) into
a **CMAF init segment** plus a running sequence of **fMP4 fragments**, cutting on
keyframes — a **remux**, not a transcode. The codecs arriving over RTMP are already
`<video>`-playable; the job is to rewrap them onto a monotonic MP4 timeline in real
time. This overlaps project 11's segmenter (`isobmff`/`segment`) — reuse what you can;
the new problem is doing it **live**, on an unbounded stream, with the timeline coming
from RTMP timestamps rather than a finished sample table.

**Done when ALL true:**
- [ ] An **init segment** (`ftyp` + `moov` carrying the `avcC`/AAC config and *zero*
  samples) is emitted once per stream and is **byte-stable** for a given codec config.
- [ ] Incoming access units are packed into `moof`+`mdat` **fragments whose `tfdt`
  `baseMediaDecodeTime` advances monotonically** across the whole live session — the
  timeline never jumps back or gaps, even as parts and segments roll.
- [ ] **Fragments (parts) are cut on demand** at a configurable part target (~200–350
  ms) and **segments start on an IDR keyframe** — a segment decodes standalone; a part
  need not, but the segment's first part carries the keyframe (marked `INDEPENDENT`).
- [ ] `init.mp4` **concatenated with a segment's parts** is a fragment a standard tool
  (`ffprobe` / `mp4box`) accepts and decodes — audio and video stay in sync across
  fragment boundaries.
- [ ] Packaging is **bounded memory**: only the current fragment's samples are held;
  finished segments past the live window are dropped — a 10-hour broadcast uses a
  few seconds of RAM, not ten hours.

**Proof:** an integration test feeding captured access units through the packager and
validating `init + parts` decodes with monotonic PTS (`fragments_decode_and_are_gapless`,
`baseMediaDecodeTime_is_monotonic`); a memory-bounded check over a long synthetic stream
(`window_bounds_memory`); `docs/13-design.md` records the box layout and the
timestamp/timescale mapping from RTMP → MP4.

*Concept to internalize:* remux vs re-encode (bitstream passthrough); CMAF chunks/parts
vs full segments; how `baseMediaDecodeTime` anchors a *live* timeline built from RTMP
message timestamps (and how you handle their 32-bit wraparound).

### V4. Low-Latency HLS playlist + blocking delivery — *break the latency wall*
In `src/llhls.rs`, generate the live media playlist and serve it with LL-HLS's
**blocking reload**, so a player sits a few hundred milliseconds behind the live edge
instead of tens of seconds. This is the vertical that turns "HLS" into "*low-latency*
HLS".

A regular live playlist is a rolling window of `#EXTINF` segments the player re-fetches
every target-duration; latency is ~3 segments. LL-HLS adds, per still-forming segment,
`#EXT-X-PART` lines (one per ~200 ms part) plus an `#EXT-X-PRELOAD-HINT` for the next
part that doesn't exist yet, an `#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES` /
`PART-HOLD-BACK`, and `#EXT-X-PART-INF:PART-TARGET`. The player then requests
`index.m3u8?_HLS_msn=N&_HLS_part=M` and the server **holds the response open** until
part `M` of media sequence `N` exists — returning immediately when it does, never a
poll-and-404 loop.

**Done when ALL true:**
- [ ] The playlist is a **valid live LL-HLS media playlist**: a rolling segment window,
  `#EXT-X-PART` per part with `DURATION`/`URI` (and `INDEPENDENT=YES` on the
  keyframe part), `#EXT-X-PRELOAD-HINT` for the forthcoming part,
  `#EXT-X-SERVER-CONTROL` (`CAN-BLOCK-RELOAD=YES`, `PART-HOLD-BACK`),
  `#EXT-X-PART-INF:PART-TARGET`, and a correct rolling `#EXT-X-MEDIA-SEQUENCE`.
- [ ] **Blocking reload works:** a request with `_HLS_msn`/`_HLS_part` for a part that
  does not exist yet **is held** and returns the updated playlist as soon as that part
  is produced — it does **not** return early with the old playlist or a 404, and a
  request for an already-available part returns immediately.
- [ ] The playlist **advances every part** (~200 ms): consecutive blocking reloads see
  the media sequence / part counters move forward monotonically, with no gap or repeat.
- [ ] A request for a **part/segment outside the live window** (already evicted, or in
  the future beyond the hint) gets a clean `404`, not a hang or a 500; a blocking wait
  has a **bounded timeout**.
- [ ] Parts and segments carry **cache headers that fit their lifetime**: the playlist
  is effectively uncacheable (changes every part), a **part** is short-lived, a
  finished **segment** and the **init** are immutable and long-cacheable.

**Proof:** unit tests that the rendered playlist contains the required LL-HLS tags and a
monotonic media sequence (`playlist_has_llhls_tags`, `media_sequence_advances`); an
integration test that a blocking `_HLS_msn/_HLS_part` request unblocks exactly when the
part is pushed and never returns stale (`blocking_reload_unblocks_on_part`); a live
end-to-end play in Safari / low-latency `hls.js`, noted in `docs/13-benchmarks.md`.

*Concept to internalize:* why segment-length latency is HLS's floor and how parts +
blocking reload get under it; the LL-HLS tag vocabulary; and the server-side concurrency
of *parking* many held requests on a single "next part ready" signal without a
thread/poll per client.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] **RTMP** is served over raw TCP (V1/V2) and interoperates with a real broadcaster
  (`ffmpeg -f flv` and/or OBS) end to end — not just against your own client.
- [ ] LL-HLS is served with **correct content types** (`application/vnd.apple.mpegurl`
  for the playlist, `video/mp4` for init, `video/iso.segment` / `video/mp4` for
  segments and parts) and **CORS** so a browser player on another origin can fetch.
  HTTP/2 is noted as the intended transport (blocking reload multiplexes many held
  GETs over one connection).
- [ ] **Graceful shutdown / stream end:** when the broadcaster disconnects (or on
  SIGTERM), the current segment is finalized and the playlist is closed with
  `#EXT-X-ENDLIST`; in-flight HTTP requests (including held blocking reloads) drain
  rather than being cut mid-response.

### Caching
- [ ] The **playlist** is served `no-store`/`no-cache` (it changes every part); a
  finished **segment** and the **init** are `Cache-Control: max-age=…, immutable` with
  a stable `ETag`; a **part** gets a short TTL — a CDN in front stays coherent.
- [ ] Built init/segment/part bytes are **memoized** in the live window (built once,
  served to every viewer) — the fan-out to N viewers does not re-mux per request.

### Security / abuse protection
- [ ] **Publish is authorized by stream key** (`LiveRegistry::authorize`): a `publish`
  to an unknown key is refused and the session closed — an open ingest lets anyone
  hijack or spoof a stream. The key is **never logged** (log a hash/prefix).
- [ ] **Inputs are bounded so a malicious publisher can't OOM/panic you:** the RTMP
  chunk size, message length, and AMF string/object sizes are range-checked before
  allocating; the number of concurrent publishers and the per-stream buffer are
  capped; a bad value ends that session, nothing else.
- [ ] **Path traversal is impossible** on the HLS side: a `key`/`msn`/`part` can never
  escape the in-memory store or a work dir (`../`, absolute, NUL) — an unknown one is a
  clean `404`, never a filesystem probe or a 500.

### Observability
- [ ] A `tracing` span per **RTMP session** (carrying a session id + hashed stream key)
  and per **HTTP request** (carrying key + the requested msn/part) — so one viewer's
  blocking reload and one publisher's session are both traceable. Never log media bytes
  or the raw key.
- [ ] Counters: publishers connected / rejected (auth), bytes ingested, segments &
  parts produced, **blocking reloads held / served / timed-out**, and viewer requests
  by kind (playlist / init / segment / part).
- [ ] Histograms/gauges: **packaging latency per part**, **live-edge age**
  (now − newest part's PTS, the glass-to-glass proxy), ingest bitrate, and **active
  publishers / held requests** — enough to watch latency creep before a viewer does.

---

## Cross-cutting scale skills
- **Bounded memory on an unbounded stream:** a live broadcast never ends on its own, so
  the segment/part window is a fixed ring — RAM tracks the window, not the airtime.
- **Fan-in / fan-out:** one publisher produces; many viewers consume the *same* built
  bytes — build once, serve N, and park held requests on a single ready-signal.
- **The live-edge timeline:** a monotonic MP4 timeline reconstructed from RTMP message
  timestamps (with 32-bit wraparound), so parts and segments never gap or jump.
- **Latency as a first-class metric:** every buffer, hold-back, and part size is a
  latency knob; you measure glass-to-glass, not just throughput.
- **Backpressure from a slow publisher:** bounded read buffers and a capped window mean
  a stalled or bursty broadcaster degrades its own stream, not the whole server.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the latency/load test lives in `bench/`,
   the numbers in `docs/13-benchmarks.md`.
3. `docs/13-design.md` records the decisions the SPEC grades: the **chunk-stream +
   handshake** handling, the **AMF/publish state machine + auth gate**, the **live
   fMP4 timeline** (RTMP→MP4 timestamp mapping, box layout, windowing), and the
   **LL-HLS part/blocking-reload** design (part target, hold-back, held-request model).
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p live-ingest` are green;
   no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Latency Wall

> Regular HLS hits a wall: three six-second segments of buffer means the viewer is
> ~15–30 seconds behind the person talking into the camera. Your job is to break
> through it — to put the viewer within a few seconds of live and *keep* them there
> while a real broadcaster pushes 1080p30 for ten straight minutes and a wall of
> players hold their playlist requests open waiting for the next 200 ms of video. If a
> part cut slips, the timeline drifts, or your blocking reloads busy-loop into 404s,
> latency balloons and the wall wins. Get under it, stay under it, and don't grow RAM
> doing it.

**Arena:** `bench/` runs a **release build** (`cargo run --release`). A real broadcaster
(`ffmpeg -re -i sample.mp4 -c copy -f flv rtmp://localhost:1935/live/boss`, or a looping
1080p30 source) publishes for **≥10 minutes**; a load generator opens **≥200 concurrent
LL-HLS players** doing blocking reloads. Latency is measured glass-to-glass via a burned-in
timecode/QR (publisher clock vs. what a player renders).

**The boss falls when ALL true:**
- [ ] **Glass-to-glass latency ≤ 3 s** (target ~2 s) at the live edge, sustained across
  the 10-minute run — not just at start.
- [ ] **Timeline integrity:** over the whole run the packaged PTS/`baseMediaDecodeTime`
  are **monotonic and gapless** across every part and segment boundary — zero jumps,
  zero gaps (prove it with `ffprobe` on a captured window, not vibes).
- [ ] **Blocking reload is real:** ≥ **95%** of `_HLS_msn/_HLS_part` requests are served
  the requested part on the **first** response (held, not 404'd), and the p99 hold time
  is within **~1 part-duration** of the part becoming available.
- [ ] **Bounded memory:** RSS stays **flat** (within the configured window) across the
  full 10 minutes — a ten-minute stream and a ten-hour stream use the same RAM.
- [ ] **Fan-out holds:** with **≥200 concurrent viewers** on one publisher, latency and
  hold-time targets above still hold, and each built part is muxed **once** (prove it
  with the packaging counter, not per-request).

**Proof:** methodology + latency distribution + the memory-over-time trace and the
blocking-reload hold-time histogram in `docs/13-benchmarks.md` (hardware + source +
`ffmpeg`/player commands reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: the RTMP listener accepts a TCP connection and the HTTP
   server answers `GET /healthz` and `GET /live` (empty) — no parsing yet.
2. Build V1: the handshake, then the chunk-stream reader — unit-test fmt 0–3 inheritance
   and a multi-chunk reassembly before a real `ffmpeg` gets past the handshake.
3. Build V2: AMF0 decode/encode + the session state machine through `publish`; get a
   real `ffmpeg` to reach the media phase, and gate it on the stream key.
4. Build V3: extract the codec config, emit the init segment, and cut the first
   keyframe-aligned fragment/part on a monotonic timeline; validate `init + part` with
   `ffprobe` (reuse project 11's box-writing where you can).
5. Build V4: render the LL-HLS playlist (parts, preload hint, server-control), then wire
   the blocking reload so a held request unblocks exactly when the part is pushed.
6. Add the auth gate + input bounds + cache headers + metrics; then point Safari /
   low-latency `hls.js` at it, measure glass-to-glass, and defeat the wall.

## Run it
```bash
cp .env.example .env          # set RTMP_PORT / HTTP_PORT / STREAM_KEYS
cargo run -p live-ingest
#   The scaffold compiles and serves. `GET /healthz` and `GET /live` work; the moment a
#   broadcaster connects, the RTMP handshake hits a todo!() and the session ends — that
#   panic is your worklist.

# Publish a live stream (needs the key to be in STREAM_KEYS):
ffmpeg -re -i sample.mp4 -c copy -f flv rtmp://localhost:1935/live/testkey

# Watch it (Safari plays LL-HLS natively; hls.js needs lowLatencyMode:true):
open http://localhost:8080/live/testkey/index.m3u8
```
