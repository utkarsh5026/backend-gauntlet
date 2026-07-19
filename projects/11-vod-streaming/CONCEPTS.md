# Concept Bank — Project 11: VOD Streaming Server (HLS/DASH)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — Reading the container: ISO-BMFF & sample tables *(V1 · `src/isobmff.rs`)*

**The problem.** You can't segment media you can't locate. An MP4 is not "video bytes" — it's a container whose index (`moov`) describes where every frame lives, how big it is, when it decodes, when it *displays* (not the same thing, thanks to B-frames), and which frames are keyframes. That description is stored as a set of cross-referenced, compressed tables — and until you can flatten them into "for every frame: offset, size, decode time, presentation time, keyframe?", the file is opaque.

**The idea.** ISO-BMFF is a tree of length-prefixed **boxes**. The `stbl` tables each encode one dimension — `stts` durations (run-length), `stsz` sizes, `stsc`+`stco` chunk geometry, `stss` keyframes, `ctts` the decode→presentation offset — and joining them yields the flat sample table everything downstream consumes. B-frames force the two-timeline insight: frames are *stored* in decode order but *shown* in presentation order, and `ctts` bridges them.

**In the wild:** this is what `ffprobe`, `mp4box`, every player's demuxer, and every packager (Shaka, Bento4) do first; the same box format underlies MP4, MOV, CMAF, and HEIF.

**You own it when you can explain:**
- [ ] The box model: how length-prefixing lets a parser skip unknown boxes (forward compatibility by construction).
- [ ] What each `stbl` table contributes, and why the format splits geometry from timing (compression: durations run-length-encode brilliantly).
- [ ] Decode order vs presentation order: why B-frames create the split, with a 3-frame (I-B-P) example showing storage vs display order.
- [ ] What a keyframe/sync sample is in codec terms (references nothing earlier) and why `stss` is load-bearing for segmentation and seeking.
- [ ] Why a parser of untrusted bytes must be total: every length checked before indexing — truncation and bit-flips produce `Err`, never a panic or OOB read.

**Depth probes:**
- Why does a progressive-download MP4 need `moov` *before* `mdat` to start playback ("faststart"), and what does that imply about how such files are written?
- `stco` vs `co64` — why does a >4 GB file break 32-bit chunk offsets, and what else in the format has the same 32/64 seam?

**Trap:** assuming presentation time ≈ decode time because your test fixture has no B-frames. The gap only appears with reordered streams — exactly the files real encoders produce.

---

## 🧠 Card 2 — Fragmented MP4: cutting media into standalone pieces *(V2 · `src/segment.rs`)*

**The problem.** A progressive MP4 is one monolith: index up front (at best), one huge `mdat`. A player can't fetch "just minute 42", can't switch quality without a new download, and a CDN can't cache useful pieces. Streaming needs the media itself restructured into pieces that decode *independently* and describe *themselves*.

**The idea.** Split the file's roles: an **init segment** (`ftyp`+`moov`) carries only codec setup — zero samples — fetched once; media ships as independent fragments (`moof`+`mdat`), each starting on a keyframe so it decodes with no predecessor. `tfdt`'s `baseMediaDecodeTime` anchors each fragment on the shared timeline, which is what makes "fetch any fragment, in any order" work. Segment duration targets bend to keyframe reality: never split a GOP to hit a target.

**In the wild:** CMAF — the converged format both HLS and DASH serve — is exactly init + fMP4 fragments; Netflix/YouTube storage is fragmented; MSE (`<video>` via JS) *only* accepts fragmented input.

**You own it when you can explain:**
- [ ] The progressive-vs-fragmented restructuring and the three capabilities it unlocks (start-before-downloaded, seek-by-fetch, mid-stream quality switch).
- [ ] Why the init segment must be sample-free and byte-stable (fetched once, cached forever, shared by every fragment).
- [ ] Why fragments must begin on keyframes — decode the failure if one starts mid-GOP.
- [ ] What `baseMediaDecodeTime` anchors and how segment N's start time relates to prior durations (drift-free by summation).
- [ ] Why packaging memory is bounded by a segment, not the asset — a 4 GB movie muxes in megabytes of RAM.

**Depth probes:**
- Why does `init.mp4 ++ segment_n.m4s` form a decodable unit for tools like ffprobe — what does each half contribute?
- Encoders sometimes emit irregular GOPs. What does your segmenter do with a 20-second GOP against a 6-second target, and what does the player experience?

**Trap:** hitting exact target durations by cutting off-keyframe "just this once". Every downstream property — standalone decode, ABR alignment, seamless switching — quietly rested on that boundary.

---

## 🧠 Card 3 — Manifests: the stream's table of contents *(V3 · `src/manifest.rs`)*

**The problem.** A pile of segments isn't a stream; the player needs an index *before* any media: what renditions exist, at what bandwidths, which segments, how long each, where to find the init. Get durations slightly wrong and errors *accumulate* — by minute 90 the player's seek math is seconds off. Get the tags wrong and real players (which are strict) simply refuse.

**The idea.** Two dialects, one shape. HLS: a **master playlist** advertises renditions (`BANDWIDTH`, `RESOLUTION`) and per-rendition **media playlists** list segments (`#EXTINF` exact durations, `#EXT-X-MAP` for init, `#EXT-X-TARGETDURATION`, `#EXT-X-ENDLIST` for VOD-complete). DASH: one XML MPD expressing the same via `SegmentTemplate`/timeline. The manifest is *declarative* — the player does all the fetching, pacing, and switching logic; your server just tells the truth accurately.

**In the wild:** every `.m3u8` you've ever seen in devtools (HLS is ~everything Apple + most live), DASH across Android/smart TVs; hls.js/dash.js implement the player half you're feeding.

**You own it when you can explain:**
- [ ] The two-level HLS structure and what a player does with each level (pick a rung; then fetch segments).
- [ ] What each required media-playlist tag controls, and what a validator/player rejects when it's missing or wrong.
- [ ] The duration-drift problem: why per-segment `#EXTINF` must be exact (not the target) and how error compounds over a long asset.
- [ ] The HLS-tags vs DASH-XML mapping — same segments, same durations, two encodings (and why the industry keeps both).
- [ ] Why VOD playlists are cacheable but live playlists (project 13) are not — the mutability difference.

**Depth probes:**
- How does a player pick its *starting* rendition from the master playlist before it has any bandwidth measurement?
- What does `#EXT-X-TARGETDURATION` bound, and what breaks if a segment exceeds it?

**Trap:** rounding durations to look clean. The manifest is arithmetic the player *trusts*; cosmetic rounding is cumulative lying.

---

## 🧠 Card 4 — Byte-range delivery & the ABR ladder *(V4 · `src/delivery.rs`)*

**The problem.** Two delivery realities: players seek (they need byte `a..b` of a resource, not all of it), and networks fluctuate (a stream that looked great on wifi must survive the elevator). The second is the deep one: quality switching can't mean re-buffering a different file — it must be seamless, mid-stream, and *client-driven*.

**The idea.** HTTP `Range` handles seeking: `206 Partial Content` + `Content-Range` for a slice, `416` for an unsatisfiable one, `Accept-Ranges: bytes` advertised. **ABR** handles fluctuation: publish the same content at several bitrates with **time-aligned segment boundaries**; the player measures throughput per segment fetch and switches rungs at boundaries — possible only because aligned segments each start on their own keyframe. The server stays dumb: plain cacheable HTTP; all adaptivity lives in the client. Determinism (same source → byte-identical segments, stable ETags) is what lets CDN caching work at all.

**In the wild:** this is *the* architecture of internet video — YouTube, Netflix, Twitch delivery are ABR ladders over HTTP ranges through CDNs; "why is my video 240p" is the client-side estimator doing its job.

**You own it when you can explain:**
- [ ] The `Range` state machine: full vs `206`-slice vs `416`, open-ended and suffix forms, and correct `Content-Range`/`Content-Length` for each.
- [ ] Why ABR's intelligence is client-side, and what that buys operationally (dumb cacheable servers, CDN-friendly, no per-viewer server state).
- [ ] Why rendition switching demands time-aligned, keyframe-started segments — trace a mid-stream 720p→1080p switch at a boundary.
- [ ] Determinism as a caching contract: re-muxing must be byte-identical or ETags and CDN caches silently diverge.
- [ ] Just-in-time packaging + memoization vs pre-packaging everything — the storage/latency trade and when each wins.

**Depth probes:**
- Which CORS headers must be *exposed* (`Content-Range`, `Accept-Ranges`, `Content-Length`) for a browser player on another origin to do range reads — and what fails silently without them?
- Why do misaligned renditions (segment 7 starts at different times in 720p vs 1080p) cause a gap or overlap on switch? Draw the timelines.

**Trap:** testing ABR with one rendition. The ladder's entire value — and its alignment constraint — only exists at ≥2 rungs; single-rendition "HLS" is just chunked download.

---

## ⚡ Rapid-fire round

- [ ] The content types that make players work: `application/vnd.apple.mpegurl`, `application/dash+xml`, `video/mp4` / `video/iso.segment`.
- [ ] `Cache-Control: immutable` + stable ETag on init/segments — why media is the perfect immutable-cache workload.
- [ ] Path traversal on a media server: asset/rendition/segment names must never escape `MEDIA_DIR`.
- [ ] Graceful shutdown drains in-flight segment streams — a mid-segment cut is a visible player error.
- [ ] The metrics that matter: cold vs memoized segment latency, cache hit ratio, range-vs-full counts.

## 🔗 Connects to

- `Range` serving is project 06's implementation, now under a player's real seek behavior.
- The fMP4 writer and keyframe discipline are *reused live* in project 13 (RTMP→LL-HLS) — same boxes, running timeline.
- The ladder you serve here is what project 12's transcode pipeline *produces* — 11 + 12 + 13 compose into project 16's platform.
