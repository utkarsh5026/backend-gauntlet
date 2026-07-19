# Concept Bank — Project 13: Live Ingest Server (RTMP → LL-HLS)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — RTMP's chunk stream: parsing a stateful binary wire *(V1 · `src/rtmp.rs`)*

**The problem.** A broadcaster's camera feed shares one TCP connection with control messages and audio. Send a 200 KB video frame as one message and every audio message queues behind it — head-of-line blocking *inside* your own connection, audible as stutter. RTMP's answer predates HTTP/2's identical answer: multiplex by shredding every message into small chunks. The cost lands on you, the parser: chunk headers are *delta-compressed against previous chunks on the same chunk-stream id* — the wire is stateful, and reassembly means tracking that state per stream, mid-connection chunk-size changes included.

**The idea.** After a byte-exact handshake (C0/C1/S0/S1/S2/C2, echoing the peer's random block — completion *is* the correctness proof, because a real ffmpeg hangs up on any error), the connection is a chunk stream: a basic header (2-bit format + variable-width chunk-stream id), then a message header whose fields (timestamp, length, type, stream id) fmt 1/2/3 progressively *inherit* from the previous chunk on that id. You reassemble chunks into complete messages, honoring extended timestamps and `Set Chunk Size`.

**In the wild:** RTMP remains the de facto broadcast-contribution protocol — OBS→Twitch/YouTube ingest is this exact wire; nginx-rtmp and SRS implement what you're building; HTTP/2 frames and QUIC streams solve the same multiplexing problem the same way.

**You own it when you can explain:**
- [ ] Why message-level interleaving over one TCP connection requires chunking — the head-of-line blocking scenario in audio/video terms.
- [ ] The fmt 0–3 inheritance scheme: which fields each format carries vs inherits, and why a steady same-size stream compresses to fmt-3 (header ≈ 1 byte).
- [ ] Why the parser is *stateful by design* — what per-chunk-stream context you must retain, and what a mid-stream `Set Chunk Size` changes.
- [ ] The extended-timestamp escape (`0xFFFFFF`) and where it must be read.
- [ ] The hostile-input discipline: every declared length range-checked before allocating — an open TCP port takes bytes from anyone.

**Depth probes:**
- Why does the handshake include 1528 random bytes echoed back? (Legacy client verification / minimal proof-of-liveness.)
- Compare RTMP chunking to HTTP/2 framing: what did each choose for stream ids, flow control, and header compression?

**Trap:** testing against captures only from your own encoder. Different broadcasters (OBS vs ffmpeg) exercise different fmt patterns and chunk sizes — interop is the spec.

---

## 🧠 Card 2 — AMF0 & the publish state machine *(V2 · `src/amf.rs`, `src/session.rs`)*

**The problem.** Past the handshake, RTMP is an RPC conversation in AMF0, and a broadcaster only starts sending media after a precise call-and-response dance. Answer wrong (or in the wrong order) and the encoder silently disconnects. Answer *too permissively* and you have a security hole: an ingest that accepts media before authenticating the stream key lets anyone hijack any stream on your service.

**The idea.** AMF0 is a compact typed serialization (1-byte marker + value: f64 number, length-prefixed string, key/value object with empty-key terminator). The session is a state machine walking `connect(app)` → `_result` → `createStream` → `_result(stream_id)` → `publish(key)` → `onStatus(Publish.Start)` — and the stream key check gates the transition into the publishing state. From the first media messages you extract the *codec configuration* (AVC sequence header → SPS/PPS/`avcC`; AAC AudioSpecificConfig) — the setup data V3 needs before it can wrap a single frame.

**In the wild:** every RTMP ingest (Twitch, YouTube, Mux) runs this exact dance; the stream key in your OBS settings is the credential this state machine checks.

**You own it when you can explain:**
- [ ] AMF0's wire format well enough to decode a `connect` by hand from hex, and why round-trip (decode∘encode = identity) is the codec's correctness bar.
- [ ] The full publish sequence, message by message, and which reply unblocks which client behavior.
- [ ] Why "state machine with an auth gate" is the design — what accepting media pre-`publish` or post-failed-auth would each permit.
- [ ] Codec config vs frame data: what the AVC sequence header contains and why it arrives once, first, instead of per-frame.
- [ ] How out-of-order or duplicate commands are handled without corrupting state (rejected or ignored — and which, documented).

**Depth probes:**
- Why does the ingest *name* the stream by key rather than URL path alone — what does key-as-credential imply about logging it (hash only)?
- What would a `releaseStream`/`FCPublish` from a client you don't support break if you crashed on unknown commands? (Forward compatibility: ignore, don't die.)

**Trap:** implementing the happy sequence only. Real encoders reconnect mid-dance, resend `publish`, and send optional commands you've never heard of — the state machine's *rejection* rows are half its value.

---

## 🧠 Card 3 — Live remuxing onto a running timeline *(V3 · `src/fmp4.rs`)*

**The problem.** Project 11 packaged a *finished* file: the sample table was complete before you cut segment one. Live has no file — frames arrive forever, the timeline comes from RTMP message timestamps (32-bit, they wrap), and you must emit valid fMP4 fragments *now*, from a stream with no end, in memory that never grows. Re-encoding is off the table: it costs a CPU core and ~100 ms+ you don't have.

**The idea.** Remux: the H.264/AAC arriving over RTMP is already `<video>`-playable — rewrap it. Emit the init segment once (from V2's codec config, byte-stable). Pack access units into `moof`+`mdat` fragments whose `tfdt` advances monotonically for the whole session — you're maintaining a running clock mapping (RTMP timestamp → MP4 timescale, unwrapping the 32-bit wrap) rather than reading a finished table. Segments start on IDR keyframes; ~200–350 ms *parts* cut inside them (a part needn't decode standalone; the segment's first part carries the keyframe). Memory is a fixed ring: hold the current fragment + the live window; drop everything older.

**In the wild:** this is the packager inside Twitch/Mux/Cloudflare Stream live pipelines; "RTMP in, LL-HLS/CMAF out, no transcode for the passthrough rung" is the standard architecture.

**You own it when you can explain:**
- [ ] Remux vs transcode at the bitstream level: what changes (container, timestamps) and what must not (the encoded payload).
- [ ] The live-timeline construction: RTMP ms → MP4 timescale mapping, why monotonicity across parts/segments is the invariant, and how 32-bit wraparound is unwrapped.
- [ ] Parts vs segments in CMAF terms: which needs a keyframe, which is a latency knob, and why ~200 ms parts.
- [ ] Bounded memory on an unbounded stream: what the ring holds, what eviction means for late viewers, and why RAM tracks the window, not airtime.
- [ ] What you reuse from project 11's box writer vs what live forces you to change (no complete `stbl`; fragment-at-a-time emission).

**Depth probes:**
- The broadcaster's clock stutters (network burst delivers 2 s of frames at once). What does your timeline mapping do — trust the RTMP timestamps or arrival time, and why?
- Why must the init segment stay byte-stable across the session? (Every viewer fetched it once; changing it strands them.)

**Trap:** deriving fragment timing from wall clock at the server. The *media* clock (RTMP timestamps) is the truth; wall-clock-based timing drifts against it and produces gap/overlap seams under any network jitter.

---

## 🧠 Card 4 — LL-HLS: blocking reload & the latency wall *(V4 · `src/llhls.rs`)*

**The problem.** Classic live HLS: 6-second segments, player buffers three → the viewer is 15–30 s behind the glass. Shrinking segments alone explodes request rates and breaks encoders. And polling faster doesn't work: a player asking "is there a new playlist yet?" every 200 ms mostly gets 304s/404s — wasted round trips that still can't beat segment-granularity latency.

**The idea.** LL-HLS attacks both ends. Publish each segment *as it forms*, in ~200 ms parts (`#EXT-X-PART`, with `INDEPENDENT=YES` on keyframe parts), advertise the not-yet-existing next part (`#EXT-X-PRELOAD-HINT`), and — the core trick — **blocking reload**: the player requests `index.m3u8?_HLS_msn=N&_HLS_part=M` and the server *holds the response open* until that part exists, answering the instant it does. Polling becomes long-polling; the wasted round trip disappears; latency drops to a couple of part-durations. Server-side, many held requests park on one "part ready" broadcast signal — not a thread or poll loop per viewer.

**In the wild:** Apple's LL-HLS (Safari native, hls.js low-latency mode), used by Twitch-class platforms to hit 2–5 s glass-to-glass; the held-request pattern is the same long-poll trick as project 21's task polling and project 16's edge.

**You own it when you can explain:**
- [ ] The latency arithmetic of classic HLS (segment duration × buffer count) and where LL-HLS's ~2 s comes from (part duration × hold-back).
- [ ] Each LL-HLS tag's role, and what `#EXT-X-MEDIA-SEQUENCE` rolling forward means for the window.
- [ ] Blocking reload end to end: the request that arrives early, what it waits on, what unblocks it, and why it must have a bounded timeout.
- [ ] The park-on-a-signal concurrency design vs thread-per-held-request — what each costs at 200 concurrent viewers.
- [ ] The cache-header ladder (uncacheable playlist / short-TTL parts / immutable segments+init) and why a CDN in front stays coherent.

**Depth probes:**
- Why does LL-HLS want HTTP/2? (Hundreds of held GETs multiplex over one connection instead of hundreds of sockets.)
- A viewer requests `_HLS_msn` far in the future (beyond the hint). Hang forever, 404, or cap? What does the spec say and why?

**Trap:** returning the *current* playlist to an early `_HLS_msn` request "so it's never empty". Returning stale defeats the entire mechanism — the player asked to be woken, not answered early.

---

## ⚡ Rapid-fire round

- [ ] Stream-end semantics: broadcaster disconnects → finalize the open segment, append `#EXT-X-ENDLIST`, drain held reloads.
- [ ] The abuse bounds a malicious publisher meets: capped chunk size, message length, AMF sizes, publisher count — each ends *that session*, nothing else.
- [ ] Live-edge age (now − newest part's PTS) as the glass-to-glass proxy metric you alert on.
- [ ] Why publishers degrade themselves, not the server: bounded read buffers + capped windows.
- [ ] Content types + CORS for cross-origin players — same list as project 11, plus the playlist changing every part.

## 🔗 Connects to

- The fMP4 machinery is project 11's segmenter running on a live timeline; the ABR ladder for live arrives when project 12's transcoder joins in project 16.
- Blocking reload reappears at the edge in project 16 (V3) — there you *hold and coalesce* for a thousand viewers.
- "Bounded window over an unbounded stream" returns in project 14's jitter buffer and project 17's recorder segments.
