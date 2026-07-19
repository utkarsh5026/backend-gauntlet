# How Live fMP4 Remuxing Works — Rewrapping a Stream That Never Ends

> A ground-up guide to turning the live H.264/AAC frames arriving off V2 into
> **CMAF fMP4** the `<video>` tag can play: what a remux is (and why it's not a
> transcode), how a fragmented MP4 differs from a normal one, and how you build a
> **monotonic timeline** out of RTMP's wrapping 32-bit millisecond timestamps —
> in memory that stays flat forever. No prior knowledge of MP4 internals
> assumed; if you built project 11's segmenter
> ([isobmff.rs](../../11-vod-streaming/src/isobmff.rs),
> [segment.rs](../../11-vod-streaming/src/segment.rs)) this will feel familiar —
> the point of this doc is what *live* changes.
>
> This prepares you for **V3** in [SPEC.md](../SPEC.md) — "Live fMP4
> repackaging" — anchored to [fmp4.rs](../src/fmp4.rs): the `CodecConfig` and
> `Sample` types (already defined), and the `build_init` / `Fragmenter::cut_part`
> `todo!()`s. Box layouts are public ISO-BMFF spec and are taught; the writer
> code and the timeline policy are yours.

---

## 0. The one sentence to hold onto

**The frames arriving over RTMP are already playable — your job is to change
their *container*, not their *content*: wrap them into an init segment plus an
endless run of `moof`+`mdat` fragments whose `baseMediaDecodeTime` never jumps
and never gaps, holding only the current fragment in memory.**

Three invariants in one sentence — passthrough bytes, monotonic timeline,
bounded memory — and they are exactly V3's five Done-when boxes regrouped.

---

## 1. Remux vs. transcode: what changes and what must not

A media file is two things glued together:

```
┌────────────────────────────────────────────────┐
│ container (the "envelope")                     │
│   timestamps · sizes · codec setup · indexing  │
│  ┌──────────────────────────────────────────┐  │
│  │ elementary streams (the "payload")       │  │
│  │   H.264 NALUs · AAC frames               │  │
│  └──────────────────────────────────────────┘  │
└────────────────────────────────────────────────┘
```

RTMP delivers the payload in FLV-tag envelopes; HLS players want fMP4
envelopes. **Transcoding** — decode to pixels, re-encode — would let you change
resolution or bitrate, but costs roughly a CPU core per stream and adds ~100 ms+
of latency, in a project whose boss fight is *measured in latency*.
**Remuxing** swaps the envelope only:

| | remux | transcode |
| --- | --- | --- |
| encoded bytes | **byte-identical passthrough** | new bytes |
| CPU | ~zero (memcpy + header math) | ~1 core / stream |
| added latency | ~0 | ~100 ms – seconds |
| can change resolution/bitrate | no | yes |
| quality loss | none | generational |

This works here for a happy structural accident: RTMP's H.264 arrives in **AVCC
framing** (length-prefixed NALUs, decoder config carried separately as the
`avcC`) — which is *exactly* the framing MP4 uses. No start-code conversion, no
bitstream surgery. The `Sample.data` you build in V2 drops into `mdat`
untouched. That's the passthrough half of the sentence; the SPEC's "no
re-encode" is not an optimization, it's the design.

(The ABR ladder — multiple renditions — is exactly what remuxing *can't* give
you; that's project 12's transcoder joining this pipeline in project 16.)

---

## 2. Why the MP4 you know can't do live — and fragments can

A classic MP4 has one `moov` box holding the **complete sample table** (`stbl`):
the offset, size, and timestamp of *every sample in the file*. Project 11 could
write it because the file was finished — the table was knowable. Live breaks
that in three independent ways:

| classic MP4 assumes | live has |
| --- | --- |
| you know all samples before writing `moov` | frames arrive forever; there is no "all" |
| the file has an end | the broadcast ends when the streamer feels like it |
| index in one place ⇒ whole file must be retained | you can only afford the last few seconds in RAM |

**Fragmented MP4** (fMP4) is the ISO-BMFF answer: move the per-sample metadata
out of `moov` into a repeating unit you can emit as you go.

```
classic:     [ftyp][        moov (all metadata)        ][      mdat (all media)      ]

fragmented:  [ftyp][moov*]  [moof][mdat]  [moof][mdat]  [moof][mdat]  ...forever
             └── init ──┘   └─ frag 1 ─┘  └─ frag 2 ─┘  └─ frag 3 ─┘
                 segment    (*moov now has ZERO samples — setup only)
```

- The **init segment** (`ftyp` + `moov`) now carries only *setup*: one `trak`
  per stream whose sample description holds the codec config — the video
  `avcC` (SPS/PPS) and the audio `esds` (AudioSpecificConfig) — plus a
  `mvex`/`trex` declaring "samples live in fragments." This is precisely why V2
  extracted [`CodecConfig`](../src/fmp4.rs) from the sequence headers: it is
  the init segment's entire content. Every viewer fetches it **once**
  (`GET /live/{key}/init.mp4`, already routed in [routes.rs](../src/routes.rs))
  and prepends it mentally to everything after.
- Each **fragment** is self-describing: `moof` (metadata for *these* samples:
  sizes, durations, flags, and a timestamp anchor) + `mdat` (their bytes).
  Decode any fragment given only the init segment and the fragments before it —
  no global table, no seeking backward, nothing retained after it's served.

CMAF is the standardization of exactly this layout so HLS and DASH share one
format; an LL-HLS **part** is one small fragment (a "CMAF chunk"), and a
**segment** is a keyframe-aligned run of parts. That's the whole vocabulary.

### 2.1 Byte-stability: why `build_init` must be a pure function

The SPEC demands the init segment be **byte-stable** for a given codec config.
The reason is the fetch-once contract: a viewer who joined at minute 3 holds
the init bytes from minute 0. If a re-render at minute 40 produced different
bytes (a timestamp field, a reordered box), viewers would be decoding new
fragments against a *different* init than new joiners — undebuggable, and it
breaks the `Cache-Control: immutable` the routes already promise. Practically:
no wall-clock creation times in `mvhd`, no randomness, nothing in the output
that isn't a function of `CodecConfig`.

---

## 3. The timeline: the actual hard part

Every fragment's `moof` contains a `tfdt` box whose one payload is
**`baseMediaDecodeTime`** — "this fragment's first sample decodes at tick N of
the movie timeline." The player lays fragments end-to-end by trusting it:

```
fragment k:    tfdt = N        durations: d₁ d₂ ... dₘ  (sum = D)
fragment k+1:  tfdt = N + D    ← must be EXACTLY this
                     └─ gap  ⇒ player stalls waiting for missing time
                     └─ overlap ⇒ player drops/glitches frames
```

That equality — *next tfdt = previous tfdt + previous fragment's total
duration* — across every part boundary for the entire session is V3's central
invariant, the `baseMediaDecodeTime_is_monotonic` test, and one of the boss
fight's own criteria (proven with `ffprobe`, "not vibes"). The scaffold's
[`Fragmenter`](../src/fmp4.rs) carries the running anchor as
`base_decode_time`, advanced at every `cut_part`.

So where do the ticks come from? You're mapping between two clocks:

| | RTMP timestamps | MP4 timeline |
| --- | --- | --- |
| unit | milliseconds | ticks of a per-movie **timescale** (`CodecConfig::timescale`, e.g. 90,000/s) |
| width | 32-bit — **wraps every 2³² ms ≈ 49.7 days** | 64-bit `tfdt` — never wraps in practice |
| origin | whatever the encoder felt like | your choice; rebase so the session starts near 0 |

Why 90 kHz instead of keeping milliseconds? Divisibility. At 30 fps a frame is
1/30 s — at 90 kHz that's exactly **3,000 ticks**; in milliseconds it's 33.33…,
so an ms-resolution stream alternates 33 and 34 (→ 2,970 / 3,060 ticks if you
convert naively per-frame). An AAC frame (1024 samples at 48 kHz) is exactly
**1,920 ticks** but 21.33 ms. The timescale exists so durations can be *exact*;
how you convert RTMP's jittery integer milliseconds into tick durations that
still sum without drift is one of the real decisions V3 leaves you.

The remaining timeline hazards, concretely:

- **32-bit wraparound.** ~49.7 days is "unreachable" until someone leaves an
  encoder running. Worse, some encoders wrap the 24-bit chunk-header field
  oddly. Treat the RTMP timestamp as *unwrappable by design*: your mapping must
  notice a jump from near-max to near-zero and keep the 64-bit timeline
  marching.
- **Whose clock is truth?** Network burst: 2 s of frames arrive in one 50 ms
  gulp. Wall-clock-at-arrival stamps them 50 ms apart → the timeline lies, A/V
  drifts, seams appear (this is CONCEPTS.md's named trap). The **RTMP
  timestamps are the media clock** — the encoder stamped them at capture. Trust
  them; treat arrival time as noise. (Then decide what a *genuinely* broken
  encoder timestamp — backwards, absurd jump — does to the session, and write
  that policy in `docs/13-design.md`.)
- **Two tracks, one timeline.** Audio and video samples interleave into
  fragments on the same clock; the "A/V stay in sync across fragment
  boundaries" Done-when box is this. Per-track `tfdt`s in each `traf` keep each
  track's running position; neither may drift against the other.

---

## 4. Cutting: parts every ~200 ms, segments on keyframes

Two nested cut cadences, driven by different constraints:

- A **part** (~200–350 ms, `IngestConfig::target_part_secs`) is the *latency*
  unit — V4 publishes each one the instant it exists. A part is just "whatever
  samples accumulated since the last cut" wrapped in `moof`+`mdat`; it does
  **not** need to be independently decodable.
- A **segment** (a few seconds, `target_segment_secs`) is the *join* unit — a
  new viewer starts decoding at a segment boundary, so a segment **must begin
  on an IDR keyframe** (`Sample::keyframe`; the part that opens a segment is
  the one V4 will mark `INDEPENDENT=YES`).

Why can't parts be the join point too? Because keyframes are *expensive* — a
keyframe every 200 ms would balloon the bitrate — and you don't control the
encoder's keyframe interval anyway. So: cut parts on a timer, open a new
segment only when a keyframe happens to arrive. Trace it at 30 fps, 2 s keyframe
interval, ~300 ms parts:

```
frames:   K f f f f f f f f f ... f   K f f f ...        (K = IDR keyframe)
parts:    [p0: K+8f][p1: 9f][p2: 9f]...[p6: 9f]‖[p0': K+8f]...
segments: ╠═══════════ segment msn=5 ═══════════╣╠═ msn=6 ...
              ▲ part 0 carries the keyframe: INDEPENDENT
```

Each cut flows into the already-wired shared window:
[`LiveStream::push_part(part, start_segment)`](../src/live.rs) — where
`start_segment: true` on a keyframe part opens the next `msn` — and
`finish_segment(full_bytes)` closes one. Note what the window stores: **built
bytes**. `cut_part` runs *once* per part regardless of viewer count; 200
viewers are 200 refcount bumps on the same `Bytes`. (The fan-out story lives in
[04-fundamentals-woven-through.md](04-fundamentals-woven-through.md).)

And this cadence is *also* the memory bound. The `Fragmenter` holds only
`pending` — the samples since the last cut, ~300 ms ≈ 100 KB at 4 Mbps — and
the window holds `window_segments` finished segments. At 4 Mbps (0.5 MB/s), a
3-segment × 2 s window is **~3 MB**, forever. Without eviction, ten hours of
broadcast is **18 GB**. The `window_bounds_memory` test and the boss fight's
flat-RSS criterion are this arithmetic made enforceable; the ring itself
(`VecDeque` + `pop_front`) is already wired in `live.rs` — your job is only to
never hold samples outside it.

### 4.1 What's inside a cut (box vocabulary, not a recipe)

`cut_part` writes one `moof` + one `mdat`. The `moof` you'll assemble from the
same box-writer discipline as project 11 — 4-byte size, 4-byte type, payload —
containing: `mfhd` (a fragment sequence number), and per track a `traf` holding
`tfhd` (track id + defaults), `tfdt` (the §3 anchor), and `trun` (the per-sample
run: count, then each sample's duration/size/flags/composition-offset, plus the
offset to where its bytes start in `mdat`). The `trun`'s composition offsets are
where `pts − dts` lives (B-frames decode before they present; the scaffold's
`Sample` carries both). Which `tfhd` defaults you use, one `trun` or several,
how you interleave the two tracks' bytes in `mdat` — those are layout decisions
the spec leaves open and `docs/13-design.md` asks you to record.

Validate with real tools, not eyeballs: `init.mp4` + parts concatenated must
satisfy `ffprobe` / `mp4box` — that's the fourth Done-when box, and it catches
box-size arithmetic errors nothing else will.

---

## 5. The design space you're deciding

- **The ms→tick conversion policy** (§3): per-sample multiply vs. accumulated
  error correction — what keeps durations summing exactly to the elapsed time?
- **Timestamp trust and repair**: what counts as a tolerable encoder stutter
  vs. a session-ending timeline violation?
- **Cut trigger placement**: who notices `target_part_secs` has accumulated —
  the session loop pushing samples, or the fragmenter itself? (Look at what
  [`Session::handle`](../src/session.rs)'s TODO threads to you.)
- **`mdat` interleaving and `trun` shape** (§4.1).
- **What you lift from project 11**: `isobmff.rs`'s writer carries over almost
  wholesale; `segment.rs`'s *logic* mostly doesn't (it walked a finished sample
  table). Knowing which is which is the reuse lesson the SPEC names.

Hard stop here — the box-writing code, the conversion math, and the cut logic
are the vertical. `/hint` for nudges, `/quest` to build it against acceptance
tests.

---

## 6. Mental model summary

| Concept | Hold onto |
| --- | --- |
| Remux | Change the envelope, never the payload; AVCC framing means H.264 passes through byte-identical |
| Init segment | `ftyp`+`moov`, zero samples, pure function of `CodecConfig`, **byte-stable**, fetched once per viewer |
| Fragment | Self-describing `moof`+`mdat`; decodable with just the init + predecessors — that's what makes live emission possible |
| `tfdt` / `baseMediaDecodeTime` | The timeline anchor; next = previous + previous duration, *exactly*, session-long |
| Timescale | Ticks/second chosen so frame durations are exact integers (90 kHz: 30 fps = 3,000, AAC@48k = 1,920) |
| RTMP clock | 32-bit ms, wraps at ~49.7 days; it is the *media truth* — map it, unwrap it, never substitute arrival time |
| Part vs segment | Part = latency unit (~200–350 ms, cut on a timer, needn't stand alone); segment = join unit (starts on IDR, `INDEPENDENT` first part) |
| Memory | `pending` (one part) + a fixed ring of `window_segments` — ~3 MB forever vs 18 GB for a 10-hour naive buffer |
| Build once | A part is muxed once into `Bytes`; N viewers share refcounts, never re-mux |

## 7. Where you'll build this

Both `todo!()`s live in [fmp4.rs](../src/fmp4.rs):

- `build_init()` — `ftyp` + `moov` from the `CodecConfig` (§2), byte-stable.
- `Fragmenter::cut_part()` — `moof`+`mdat` from `pending`, advancing
  `base_decode_time` (§3–4).

They're fed by your V2 dispatcher ([`Session::handle`](../src/session.rs)) and
their output lands in the wired window ([`LiveStream`](../src/live.rs)) via
`set_init` / `push_part` / `finish_segment`, where V4 will serve it.

This doc unlocks V3's **Done when ALL true** ([SPEC.md](../SPEC.md)):
byte-stable init · monotonic `tfdt` across the session · parts on a
configurable target, segments on IDR · `init + parts` passes `ffprobe` with A/V
in sync · bounded memory. Proof: `fragments_decode_and_are_gapless`,
`baseMediaDecodeTime_is_monotonic`, `window_bounds_memory`, and the box-layout
+ timestamp-mapping write-up in `docs/13-design.md`.
