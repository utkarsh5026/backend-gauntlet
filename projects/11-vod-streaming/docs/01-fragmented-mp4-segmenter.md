# Fragmented MP4: Cutting Media into Standalone Pieces — From First Principles

> A beginner-friendly guide. **No prior knowledge assumed** beyond
> [doc 00 (the box tree & sample table)](./00-iso-bmff-and-sample-tables.md).
> This teaches the *idea* behind **V2**, the marquee vertical, so you can write the
> segmenter yourself. It prepares you for [`src/segment.rs`](../src/segment.rs) —
> the `plan_segments()`, `build_init_segment()`, and `build_media_segment()`
> `todo!()`s — and the V2 checklist in [`SPEC.md`](../SPEC.md). It teaches the *shape*
> of the fMP4 boxes and the segmentation *policy*; it does **not** write them for you.

---

## The one sentence to hold onto

**Restructure the file so its media becomes a run of pieces that each decode alone
and describe their own place on the timeline — an `init` segment of pure setup, then
independent `moof`+`mdat` fragments, each starting on a keyframe.**

---

## 1. The problem: a progressive mp4 is a monolith

After V1 you can *read* any mp4. But a normal ("progressive") mp4 is one big lump:

```
[ ftyp ][ moov = the whole index ][ mdat = ALL the media, one blob ]
```

Now try to build a streaming server on top of that. Every real requirement breaks:

| A player wants to… | With a progressive mp4… |
|--------------------|-------------------------|
| Start playing before the file is downloaded | Can't — needs `moov`, and often it's at the end |
| Fetch *just* "minute 42" | Can't — there's no unit smaller than the file |
| Switch to a lower quality mid-stream | Can't — that's a *different file*; start over |
| Let a CDN cache useful pieces | Can't — the cacheable unit is the whole movie |

You could paper over *seeking* with HTTP byte ranges (that's V4) — but ranges give you
"bytes 5,000,000–5,900,000", not "a chunk that decodes on its own." The deeper problem
is that the media itself has no **independently decodable pieces**. Streaming needs the
bytes *restructured*, not just addressed.

---

## 2. The idea: split roles, then split media into fragments

Fragmented MP4 (fMP4) makes two moves.

**Move 1 — separate setup from media.** Pull everything a decoder needs to *start*
(codec config, resolution, timescale — but **zero samples**) into a tiny **init
segment**, fetched once and cached forever:

```
init segment  =  [ ftyp ][ moov (codec setup, mvex/trex, NO samples) ]
```

**Move 2 — ship media as independent fragments.** Each **media segment** is a small,
self-describing unit:

```
media segment =  [ styp ][ moof (this fragment's index) ][ mdat (its frames) ]
```

The contrast with progressive is exact:

```
Progressive:   ftyp | moov(index for EVERYTHING) | mdat(ALL media)

Fragmented:    ftyp | moov(setup, 0 samples)              ← init, fetched once
               styp | moof(index for seg 0) | mdat(seg 0) ← segment 0
               styp | moof(index for seg 1) | mdat(seg 1) ← segment 1
               styp | moof(index for seg 2) | mdat(seg 2) ← ...
```

The single giant `moov` is replaced by a setup-only `moov` **plus** one small `moof`
("movie fragment") per segment. That restructuring is what unlocks all three
capabilities the progressive file couldn't offer:

1. **Start before downloaded** — grab init + segment 0, play.
2. **Seek by fetch** — jump straight to segment 7's bytes.
3. **Switch quality mid-stream** — next segment comes from a different rendition
   (V4's ABR).

> **In the wild:** this is **CMAF** — the one fragmented format both HLS and DASH
> serve today. Netflix/YouTube storage is fragmented; and MSE (`<video>` fed by
> JavaScript, i.e. hls.js) *only* accepts fragmented input. Progressive mp4 in a
> `<video src>` works, but you can't build adaptive streaming on it.

---

## 3. Anatomy of an init segment — and why it must be byte-stable

The init segment is a `moov` that carries the codec description you extracted in V1
(`track.codec.setup` — the `avcC` with SPS/PPS) but **no sample tables of media**.
Instead it contains one new box family that says "the real samples live in
fragments":

```
moov
 ├── mvhd                     movie header
 ├── trak → … → stbl          codec setup (stsd) but EMPTY sample tables
 └── mvex                     "movie extends" — the fragmentation declaration
      └── trex                default sample settings for fragments to inherit
```

`mvex`/`trex` is the switch that flips the file from "all media is here in `moov`" to
"media arrives later, in `moof`s." Without it, players treat the `moov` as complete
and see an empty movie.

**Why byte-for-byte identical across requests matters.** The init is fetched once and
cached forever (V4 will slap `Cache-Control: immutable` + an `ETag` on it). If two
requests for the same rendition's init produce *different* bytes — because you stamped
"now" into `mvhd`'s creation time, or used a random track UUID — then the `ETag`
changes, caches diverge, and you've broken the caching contract for no reason. So the
scaffold's `build_init_segment()` note is blunt: *no timestamps-of-day, no random ids;
determinism is a graded criterion.* Use fixed/zero values for anything time-of-day.

---

## 4. Anatomy of a media segment — the `moof` mini-index

Each media segment carries its *own* little index (`moof`) describing just its frames,
then the frames themselves (`mdat`):

```
styp                         segment type (brands, like ftyp)
moof
 ├── mfhd  sequence_number    monotonically increasing (segment N → N+1)
 └── traf                     track fragment
      ├── tfhd  track_id, default flags
      ├── tfdt  baseMediaDecodeTime   ← the timeline anchor (§5)
      └── trun  per-sample: size, duration, composition_offset, + a data_offset
mdat                         the coded bytes, copied from the source at V1's offsets
```

Two things are easy to get subtly wrong, and the scaffold calls both out:

- **`trun`'s `data_offset`** must equal the byte distance from the *start of the
  `moof`* to the *first byte of `mdat`'s payload*. The player uses it to find where
  each sample begins. Off by a few bytes → the decoder reads garbage. You only know
  this offset after you've laid out the whole `moof`, so it's computed last.

- **The `mdat` bytes come from V1.** For each sample in the segment you copy
  `source[sample.offset .. sample.offset + sample.size]`, in order. `build_media_segment`
  receives `source: &[u8]` and the `entry.samples` range for exactly this.

### Why memory stays bounded (a graded criterion)

You copy **only this segment's** bytes into the `mdat`. A 4 GB movie has ~600 six-second
segments; you build one at a time, so peak memory is *one segment* (a few MB), not the
asset. The SPEC's cross-cutting skill — *"a 4 GB movie packages in a segment's worth of
RAM"* — falls out of this discipline automatically, as long as you never load or hold
the whole media.

---

## 5. `baseMediaDecodeTime`: the anchor that makes a fragment standalone

Here's the subtle question: if segment 7's `mdat` just contains frames, how does a
player know those frames belong at *minute 0:42* and not *minute 0:00*?

Answer: **`tfdt`'s `baseMediaDecodeTime`** — the decode time (in the track's
timescale) of the segment's *first* sample. It anchors the fragment on the shared
timeline. Because of it, a player can fetch segment 7 **cold, in isolation** — no need
to have seen segments 0–6 — and still place its frames correctly. That's what "seek by
fetch" actually rides on.

And the anchor must be **drift-free by summation**:

```
segment N's start_time  ==  sum of all prior segments' durations
start_time[n+1]  ==  start_time[n] + duration[n]      (gapless, no rounding)
```

The scaffold's [`SegmentEntry`](../src/segment.rs) already models this: `start_time`
"equals the summed durations of all prior segments," and the `plan_segments` TODO
requires consecutive segments be gapless. Compute in the **integer timescale**, never
in floating seconds — summing floats accumulates error and your anchors drift.

---

## 6. The segmentation policy: **the keyframe boundary always wins**

This is the decision at the heart of V2, and the SPEC deliberately leaves the *how* to
you. The shape of the problem:

You have a **target** duration (say ~6 s, `DEFAULT_TARGET_SEGMENT_SECS` in
[`main.rs`](../src/main.rs)). You want segments near that length. **But** every segment
must *begin on a keyframe* so it decodes standalone (doc 00, §6). Keyframes occur only
at GOP boundaries — maybe every 2 s, maybe every 10 s, whatever the encoder chose. So
the target and the keyframes fight, and one has to yield.

**The rule: the keyframe wins, always.** You accumulate whole GOPs (keyframe → just
before the next keyframe) and cut when adding the *next* GOP would push you past the
target. You never split a GOP to hit 6.000 s exactly.

```
Keyframes at:  0s      2s      4s      6.5s        8.5s     ...
GOPs:          |--G0--|--G1--|--G2--|----G3----|----G4----|
Target = 6s:
  Segment 0 = G0+G1+G2  = 6.5s   (adding G3 → 8.5s, too far past 6 → cut at 6.5s)
  Segment 1 = G3+G4     = ...
```

Segment 0 is **6.5 s, not 6.0 s** — and that's correct. Real segment lengths *cluster
around* the target, never sit exactly on it.

> **The trap (from the concept card):** hitting an exact 6.000 s by cutting
> off-keyframe "just this once." That one shortcut silently breaks *every* downstream
> property — standalone decode, ABR alignment (V4), seamless switching, seek accuracy —
> all of which quietly rested on the keyframe boundary. There is no "just this once."

**Depth probe worth chewing on:** an encoder emits a pathological 20-second GOP against
your 6-second target. What does your segmenter do? (It *must* emit a single ~20 s
segment — the keyframe rule admits no other choice — which is also why V3's
`#EXT-X-TARGETDURATION` has to be `ceil(longest segment)`, not the target. This is the
real reason accurate per-segment durations matter, picked up in doc 02.)

The edge case the scaffold names explicitly: **if the source flags no sync samples at
all**, treat *every* sample as a valid boundary (intra-only / all-keyframe content).

---

## 7. Why `init ++ segment` is a decodable unit (the V2 proof)

The graded proof is: `init.mp4` concatenated with **any one** media segment is a file
`ffprobe`/`mp4box -info` accepts and decodes. Think about *why* that works, because it
tells you whether your boxes are right:

- **init** contributes: the codec setup (how to interpret the bytes), the timescale,
  and the `mvex` "expect fragments" declaration.
- **segment** contributes: a `moof` saying "here are N samples, this many bytes each,
  anchored at time T" and an `mdat` with exactly those bytes.

Together they form a complete, minimal fragmented movie — the smallest playable thing.
If a validator rejects `init ++ seg`, the mismatch (a wrong `data_offset`, a `trun`
count that disagrees with `mdat`, a missing `trex`) is your bug list.

---

## Mental model summary

| Thing | One-liner |
|-------|-----------|
| Progressive mp4 | one `moov` + one `mdat`; a monolith — can't stream/seek/switch |
| Fragmented mp4 | init (setup, 0 samples) + independent `moof`+`mdat` fragments |
| init segment | fetched once, cached forever, **must be byte-identical** |
| `mvex`/`trex` | the box that declares "media lives in fragments" |
| `moof` | a fragment's own mini-index (`mfhd`/`tfhd`/`tfdt`/`trun`) |
| `tfdt` baseMediaDecodeTime | the timeline anchor; = sum of prior durations, gapless, integer math |
| `trun` data_offset | moof-start → first mdat byte; wrong = garbage playback |
| Segmentation rule | keyframe boundary beats the target — always; never split a GOP |
| Bounded memory | copy one segment's bytes; 4 GB movie muxes in MB |

## Where you'll build this

[`src/segment.rs`](../src/segment.rs):
- `plan_segments()` — the keyframe-aligned grouping policy (§6).
- `build_init_segment()` — `ftyp` + setup-only `moov` + `mvex`/`trex`, deterministic (§3).
- `build_media_segment()` — `styp` + `moof` + `mdat` for one entry (§4–5).

**This doc unlocks these V2 "Done when ALL true" boxes:** a byte-identical init
segment; every segment begins on a keyframe; `trun`/`tfdt` consistent with source
timing (start = sum of priors); durations track the target without splitting a GOP;
`init ++ seg` decodes in a real tool; memory bounded by segment size.

**The interesting decisions are yours:** the exact box byte-layout you emit (record it
in `docs/11-design.md` — that's a graded deliverable), your precise "close the
segment" condition, and how you lay out the `moof` to compute `data_offset` last. Those
are the build — use [`/hint`](../../..) for a nudge and [`/quest`](../../..) for a
guided, acceptance-test-first run at V2.
