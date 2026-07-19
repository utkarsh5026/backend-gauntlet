<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 11 — VOD Streaming Server (HLS/DASH)

> "Serve a video file over HTTP." A `GET` that returns an `.mp4` *is* that — and it
> works right up until a real player, a real network, or a real seek touches it.
> A progressive MP4 puts its index (`moov`) and its media (`mdat`) in two big
> blobs, so a player can't start until it has fetched enough of the file, can't
> seek without a round trip to re-read the index, and can't switch quality without
> throwing the whole download away. The web's answer — the thing YouTube, Netflix
> and every `<video>` tag actually do — is to **not** ship one file. You cut the
> media into a few-second **segments**, each starting on a keyframe so it decodes
> standalone, wrap them in **fragmented MP4** (an `init` segment of setup + many
> `moof`+`mdat` fragments), and publish a **manifest** (`.m3u8` for HLS, `.mpd` for
> DASH) that lists the segments and their durations. The player reads the manifest,
> fetches segments over plain cacheable HTTP, and — because you publish the *same*
> content at several bitrates with **aligned** segment boundaries — swaps up or down
> the quality **ladder** mid-stream as bandwidth changes (**ABR**). None of that is
> a library call here: you hand-parse the ISO Base Media File Format box tree to
> find where every frame lives and when it plays, you hand-write the fMP4 boxes for
> the init and media segments, you generate the manifests yourself, and you serve
> segments (and seeks) with HTTP **byte-range** requests. It's `read(file)` turned
> into a demux, a mux, a packaging format, and a delivery protocol. That's the rung.

## What it does (the easy part)
- Loads a **media library** from disk: `MEDIA_DIR/<asset>/<rendition>.mp4` (e.g.
  `media/bbb/1080p.mp4`, `media/bbb/720p.mp4`), scanned at startup.
- Serves a **HLS** master playlist `GET /vod/{asset}/master.m3u8`, a per-rendition
  media playlist `GET /vod/{asset}/{rendition}/index.m3u8`, a CMAF init segment
  `GET /vod/{asset}/{rendition}/init.mp4`, and media segments
  `GET /vod/{asset}/{rendition}/seg/{n}` — the last served with HTTP `Range`.
- Serves a **DASH** manifest `GET /vod/{asset}/manifest.mpd` describing the same
  segments.
- `GET /assets` lists the library; `GET /healthz` is liveness.

> There is **no database and no docker-compose** here: the filesystem *is* the
> source, and the packager builds everything else on demand. The parts you'd
> normally hand to `ffmpeg` / `GStreamer` / Shaka Packager — reading the container,
> cutting keyframe-aligned fragments, writing the fMP4 boxes, emitting the
> manifests — are exactly the parts you build. The whole point is that "an HLS
> server" is a demux + a mux + a manifest over plain files, not a service you call.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. ISO-BMFF demuxer — *read the container by hand*
In `src/isobmff.rs`, parse the source MP4's box tree and reduce it to a normalized
**sample table**: for every frame, where it lives in the file, when it decodes and
presents, and whether it's a keyframe. You can't segment media you can't locate,
and this is the layer `ffmpeg`'s demuxer would hand you.

An MP4 is a tree of length-prefixed **boxes** (`ftyp`, `moov` → `trak` → `mdia` →
`minf` → `stbl`). The `stbl` sample tables (`stsd`, `stts`, `stsc`, `stsz`,
`stco`/`co64`, `stss`, `ctts`) are a *compressed, cross-referenced* description of
the media — decoding them into one flat per-sample list is the work.

**Done when ALL true:**
- [ ] Parsing a well-formed MP4 yields, per track, its **timescale**, codec, and a
  **sample table** — for every sample: byte offset in the file, size, decode time,
  duration, and whether it is a **sync sample** (keyframe).
- [ ] The parsed **sample count and total duration match the source** (a known
  fixture's frame count and duration are reproduced exactly).
- [ ] Both 32-bit chunk offsets (`stco`) and 64-bit (`co64`) are handled — a file
  using either parses.
- [ ] **Presentation vs decode order is preserved:** `ctts` composition offsets are
  applied so a reordered (B-frame) stream's presentation time is recoverable, not
  assumed equal to decode time.
- [ ] The **codec initialization data** (e.g. `avcC` / SPS+PPS, plus width/height)
  needed to later build an init segment is extracted and retained.
- [ ] A **truncated or malformed box** is rejected with an error — never a panic, an
  overflow, or an out-of-bounds read.

**Proof:** unit tests over a small committed fixture MP4 asserting frame count,
duration, and keyframe positions (`parses_fixture_sample_table`); a property/fuzz
test that random truncations & byte-flips never panic the parser
(`malformed_input_never_panics`).

*Concept to internalize:* the box/atom structure of ISO-BMFF; how the `stbl` tables
encode sample geometry and timing separately (and why); and decode-time vs
presentation-time (`ctts`) reordering.

### V2. The fMP4 / CMAF segmenter — *write the boxes by hand*
In `src/segment.rs`, turn the sample table into a **CMAF init segment** plus
**keyframe-aligned media segments** — the mux step. This is the marquee vertical.

Progressive MP4 (`moov` + one `mdat`) can't be sliced or streamed. Fragmented MP4
is an **init segment** (`ftyp` + `moov` carrying codec setup, *zero* samples) and a
run of independent **media segments** (`styp` + `moof` + `mdat`), each beginning on
a keyframe so it decodes without its predecessor. `tfdt`'s `baseMediaDecodeTime` is
the timeline anchor that makes each segment playable standalone.

**Done when ALL true:**
- [ ] An **init segment** (`ftyp` + `moov` with the codec config and no samples) is
  produced, and is **byte-for-byte identical** across repeated requests for the same
  rendition.
- [ ] Media is cut into segments that **each begin on a keyframe** — no segment
  starts mid-GOP, so any single segment decodes on its own.
- [ ] Each media segment is a valid fragment (`moof` + `mdat`) whose `trun` sample
  sizes/durations and `tfdt` base decode time are **consistent with the source
  timing**: segment *N*'s start time equals the sum of the prior segments' durations.
- [ ] Segment durations track a **configurable target** (e.g. ~6 s) *without ever
  splitting a GOP* to hit it — the keyframe boundary wins over the target.
- [ ] `init.mp4` **concatenated with any one media segment** is a fragment a standard
  tool (`ffprobe` / `mp4box -info`) accepts and can decode.
- [ ] Packaging holds **no media bytes in memory beyond the current segment** —
  memory is bounded by segment size, not asset size.

**Proof:** an integration test / `bench/` run feeding `init + seg` to a validator
(`ffprobe` or a box-tree assertion) showing a decodable, keyframe-aligned fragment
(`init_plus_segment_is_decodable`); `docs/11-design.md` records the exact box layout
you emit and the target-duration policy.

*Concept to internalize:* progressive vs fragmented MP4; why segments must start on
keyframes; the `moof`/`traf`/`tfhd`/`tfdt`/`trun` fragment layout and what
`baseMediaDecodeTime` buys you.

### V3. Manifest generation — *HLS `.m3u8` + DASH `.mpd`*
In `src/manifest.rs`, generate the indexes a player reads before any media: the HLS
media & master playlists and the DASH MPD, computed from V2's segment list. The
manifest is where "a pile of segments" becomes "a playable stream".

**Done when ALL true:**
- [ ] A **HLS media playlist** lists every segment with an accurate `#EXTINF`
  duration, references the init segment via `#EXT-X-MAP`, declares
  `#EXT-X-TARGETDURATION` ≥ the longest segment, and is marked VOD-complete with
  `#EXT-X-ENDLIST`.
- [ ] A **HLS master playlist** advertises each rendition with its `BANDWIDTH` and
  `RESOLUTION` so a player can pick a starting rung and switch.
- [ ] A **DASH MPD** describes the same segments (SegmentTemplate/Timeline or
  SegmentList) with matching durations and an init reference.
- [ ] Summed `#EXTINF` durations equal the asset's total duration **within one
  frame** — no rounding drift accumulates across a long asset.
- [ ] The playlists are **spec-valid**: a conformance validator / a real player loads
  them without error.

**Proof:** golden-file tests comparing generated playlists to committed expected
output for the fixture (`renders_hls_media_playlist`, `renders_dash_mpd`); a
validation run (Apple `mediastreamvalidator` and/or a DASH validator) noted in
`docs/11-benchmarks.md`.

*Concept to internalize:* the manifest as the stream's index; HLS's tag vocabulary
vs DASH's XML/`SegmentTemplate` model; and why accurate per-segment durations (not
just the target) matter for seeking and drift.

### V4. Byte-range delivery + the ABR ladder — *seek and adapt over HTTP*
In `src/delivery.rs`, serve media with HTTP **`Range`** requests and wire the
**adaptive-bitrate ladder** so a player can seek and switch quality. Range serving
is what makes video seek and single-file packaging possible; ABR is what makes the
whole "many renditions" structure pay off.

**Done when ALL true:**
- [ ] A `Range: bytes=a-b` GET returns **`206 Partial Content`** with a correct
  `Content-Range` and only the requested slice; the same URL with no `Range` returns
  the whole resource as `200`.
- [ ] An **open-ended** (`bytes=a-`) and a **suffix** (`bytes=-n`) range both resolve
  correctly; an **unsatisfiable** range (start past EOF) returns **`416`** with
  `Content-Range: bytes */<len>`.
- [ ] Media responses advertise `Accept-Ranges: bytes` and a `Content-Length` that
  matches the bytes actually returned (full or slice).
- [ ] The **ABR ladder is real:** the master playlist lists **≥2 renditions** whose
  segment boundaries **align in time**, so a player can switch renditions at any
  segment boundary without a gap or overlap.
- [ ] A media body is **streamed, not buffered whole** on the way out — serving a
  range costs memory bounded by a chunk, not by the segment.

**Proof:** integration tests asserting `206` / `416` / `Content-Range` for
representative ranges (`range_request_returns_206_slice`,
`unsatisfiable_range_returns_416`); a `bench/` run driving a real player
(`hls.js` / `ffmpeg`) through a **rendition switch**, noted in
`docs/11-benchmarks.md`.

*Concept to internalize:* HTTP `Range`/`206`/`416` semantics and `Content-Range`;
why byte-range serving underpins both seeking and single-file packaging; and why ABR
switching only works when renditions share aligned, independently-decodable segments.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] **Correct content types**: `application/vnd.apple.mpegurl` (`.m3u8`),
  `application/dash+xml` (`.mpd`), `video/mp4` (init), `video/iso.segment` or
  `video/mp4` (media segments).
- [ ] `Range` → `206`/`416` semantics correct (V4), with `Accept-Ranges: bytes` on
  every media response.
- [ ] **CORS** configured so a browser player (`hls.js`/`dash.js`) on another origin
  can fetch — including **exposing** `Content-Range`, `Content-Length`,
  `Accept-Ranges` so range reads work cross-origin.
- [ ] **Graceful shutdown** drains in-flight segment streams on SIGTERM (no
  mid-segment connection drops).

### Caching
- [ ] Immutable media (init + segments) served with a long-lived
  `Cache-Control: max-age=…, immutable` and a stable `ETag`; a conditional
  `If-None-Match` gets `304`. VOD playlists are cacheable too.
- [ ] Generated init/segments are **memoized** (cut once → reuse) rather than
  re-muxed per request — the same request yields the same bytes and the same `ETag`.

### Security / abuse protection
- [ ] **Path traversal is impossible:** an `asset`/`rendition`/segment index can
  never escape `MEDIA_DIR` (`../`, absolute paths, symlinks); an unknown asset is a
  clean `404`, not a filesystem probe or a 500.
- [ ] Inputs are **validated & bounded**: the `Range` header syntax, the segment
  index (reject out-of-range), and rendition/asset names — a malformed request is a
  `400`/`404`, never a panic.
- [ ] **(Stretch) signed/expiring URLs** or a token gate on playlists — a taste of
  CDN access control. Note the DRM/at-rest boundary you are explicitly *not* doing.

### Observability
- [ ] A `tracing` span per request (via `common-telemetry`) carrying `asset`,
  `rendition`, and — for media — the byte range served. Never log media bytes.
- [ ] Counters: playlists served (master/media/mpd), init & segment requests, **range
  vs full** responses, `416`s, and segment cache **hit/miss**.
- [ ] Histograms: **segment-generation time** (cold cut) and segment size; a gauge for
  assets/renditions loaded.

---

## Cross-cutting scale skills
- **Bounded memory:** segment-at-a-time muxing and chunked range serving keep RSS
  independent of asset size — a 4 GB movie packages in a segment's worth of RAM.
- **Just-in-time vs pre-packaged:** cut segments on demand and **memoize** them — the
  latency/storage tradeoff every real packager makes.
- **Determinism as a caching contract:** the same source yields **byte-identical**
  init/segments, so an `ETag` and any cache in front stay coherent.
- **Backpressure:** a slow client pulls range bytes at its own rate; you never buffer
  a whole asset to feed it.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains numbers: **segment-generation throughput** (segments/s and
   MB/s), **first-byte latency** for a cold vs. memoized segment, and a **real player
   playing through** — `ffmpeg`/`hls.js` pulls the master, plays start → `ENDLIST`,
   and performs a **rendition switch** — recorded in `docs/11-benchmarks.md`.
3. `docs/11-design.md` records the decisions the SPEC grades: the **box layout** you
   emit (init + `moof`/`mdat`), the **keyframe-aligned segmentation** rule and
   target-duration policy, the **HLS↔DASH mapping**, the **byte-range + ABR-alignment**
   design, and the **memoization/caching** model.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p vod-streaming` are
   green; no `todo!()` remains on a checked path.

## Suggested order of attack
1. Get the boring path working: `Catalog::load` scans `MEDIA_DIR`, `GET /healthz` and
   `GET /assets` list the library — no packaging yet.
2. Build V1: parse the box tree and the `stbl` sample tables into one flat per-sample
   list; unit-test frame count, duration, and keyframe positions against a committed
   fixture.
3. Build V2: emit the init segment, then cut **one** keyframe-aligned media segment;
   validate `init + seg` decodes with `ffprobe`.
4. Build V3: generate the HLS media playlist from the segment list, then the master
   and the DASH MPD; validate them.
5. Build V4: add `Range` → `206`/`416` serving, then memoize cut segments; add a
   second rendition and align its segment boundaries so ABR switching is seamless.
6. Add CORS + cache/ETag headers + traversal guards + metrics; point `hls.js`/`ffmpeg`
   at it, benchmark, and document.

## Run it
```bash
cp .env.example .env          # set MEDIA_DIR (source MP4s) + PORT
# Drop a source file at $MEDIA_DIR/<asset>/<rendition>.mp4, e.g.:
#   media/bbb/1080p.mp4  media/bbb/720p.mp4
cargo run -p vod-streaming
#   The scaffold compiles and serves. `GET /healthz` and `GET /assets` work; the
#   first playlist/segment request hits a todo!() in V1–V4 — that panic is the worklist.

# Once V1–V3 are done, point a player at the master playlist:
ffplay  http://localhost:8080/vod/bbb/master.m3u8
#   or load it in an <video> tag with hls.js.
```
