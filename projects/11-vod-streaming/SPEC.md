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
- Serves a **DASH** manifest `GET /vod/{asset}/{rendition}/manifest.mpd` describing
  the same segments.
- `GET /assets` lists the library; `GET /healthz` is liveness; `GET /metrics` is
  the Prometheus scrape.

> There is **no database and no docker-compose** here: the filesystem *is* the
> source, and the packager builds everything else on demand. The parts you'd
> normally hand to `ffmpeg` / `GStreamer` / Shaka Packager — reading the container,
> cutting keyframe-aligned fragments, writing the fMP4 boxes, emitting the
> manifests — are exactly the parts you build. The whole point is that "an HLS
> server" is a demux + a mux + a manifest over plain files, not a service you call.
>
> The dependency list makes the same point by omission: no `pymp4`, no PyAV, no
> `ffmpeg-python`. What you get is `struct` for the big-endian wire format, `mmap`
> so a 4 GB source is addressable without being resident, and `memoryview` so
> slicing it costs nothing — all three from the standard library.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. ISO-BMFF demuxer — *read the container by hand*
In `src/vod_streaming/isobmff.py`, parse the source MP4's box tree and reduce it to
a normalized **sample table**: for every frame, where it lives in the file, when it
decodes and presents, and whether it's a keyframe. You can't segment media you
can't locate, and this is the layer `ffmpeg`'s demuxer would hand you.

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
  assumed equal to decode time — including the **signed** offsets a version-1
  `ctts` carries.
- [ ] The **codec initialization data** (e.g. `avcC` / SPS+PPS, plus width/height)
  needed to later build an init segment is extracted and retained — as `bytes`, so
  nothing outlives the mapping it was read from.
- [ ] A **truncated or malformed box is rejected with an error**, never a wrong
  answer: a short slice must not silently become a plausible number, a declared box
  size must not be trusted past the end of the buffer, and no `struct.error`,
  `IndexError` or unbounded allocation may escape to the caller.

**Proof:** unit tests over a small committed fixture MP4 asserting frame count,
duration, and keyframe positions (`test_parses_fixture_sample_table`); a
**Hypothesis** property test that random truncations & byte-flips always raise
`MalformedMedia` and never anything else (`test_malformed_input_always_raises`).

*Concept to internalize:* the box/atom structure of ISO-BMFF; how the `stbl` tables
encode sample geometry and timing separately (and why); and decode-time vs
presentation-time (`ctts`) reordering.

### V2. The fMP4 / CMAF segmenter — *write the boxes by hand*
In `src/vod_streaming/segment.py`, turn the sample table into a **CMAF init
segment** plus **keyframe-aligned media segments** — the mux step. This is the
marquee vertical.

Progressive MP4 (`moov` + one `mdat`) can't be sliced or streamed. Fragmented MP4
is an **init segment** (`ftyp` + `moov` carrying codec setup, *zero* samples) and a
run of independent **media segments** (`styp` + `moof` + `mdat`), each beginning on
a keyframe so it decodes without its predecessor. `tfdt`'s `baseMediaDecodeTime` is
the timeline anchor that makes each segment playable standalone.

**Done when ALL true:**
- [ ] An **init segment** (`ftyp` + `moov` with the codec config and no samples) is
  produced, and is **byte-for-byte identical** across repeated requests for the same
  rendition — and across *processes*, so nothing derives from `hash()`, `id()` or
  the wall clock.
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
  RSS while cutting segment *N* of a multi-gigabyte asset is bounded by the segment,
  not the asset, and `memray` says so rather than you assuming it.

**Proof:** an integration test / `bench/` run feeding `init + seg` to a validator
(`ffprobe` or a box-tree assertion) showing a decodable, keyframe-aligned fragment
(`test_init_plus_segment_is_decodable`, and `make validate` for the eyeball
version); `docs/11-design.md` records the exact box layout you emit and the
target-duration policy.

*Concept to internalize:* progressive vs fragmented MP4; why segments must start on
keyframes; the `moof`/`traf`/`tfhd`/`tfdt`/`trun` fragment layout and what
`baseMediaDecodeTime` buys you.

### V3. Manifest generation — *HLS `.m3u8` + DASH `.mpd`*
In `src/vod_streaming/manifest.py`, generate the indexes a player reads before any
media: the HLS media & master playlists and the DASH MPD, computed from V2's
segment list. The manifest is where "a pile of segments" becomes "a playable
stream".

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
  them without error, and the MPD survives an asset name containing `&` or `<`
  (i.e. the XML is generated, not string-formatted).

**Proof:** golden-file tests comparing generated playlists to committed expected
output for the fixture (`test_renders_hls_media_playlist`, `test_renders_dash_mpd`);
a validation run (Apple `mediastreamvalidator` and/or a DASH validator) noted in
`docs/11-benchmarks.md`.

*Concept to internalize:* the manifest as the stream's index; HLS's tag vocabulary
vs DASH's XML/`SegmentTemplate` model; and why accurate per-segment durations (not
just the target) matter for seeking and drift.

### V4. Byte-range delivery + the ABR ladder — *seek and adapt over HTTP*
In `src/vod_streaming/delivery.py`, serve media with HTTP **`Range`** requests and
wire the **adaptive-bitrate ladder** so a player can seek and switch quality. Range
serving is what makes video seek and single-file packaging possible; ABR is what
makes the whole "many renditions" structure pay off.

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
  range costs memory bounded by a chunk, not by the segment, and a slow client pulls
  at its own rate rather than being buffered for.

**Proof:** integration tests asserting `206` / `416` / `Content-Range` for
representative ranges (`test_range_request_returns_206_slice`,
`test_unsatisfiable_range_returns_416`, and `make range` for the eyeball version);
a `bench/` run driving a real player (`hls.js` / `ffmpeg`) through a **rendition
switch**, noted in `docs/11-benchmarks.md`.

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
- [ ] **Graceful shutdown** drains in-flight segment streams on SIGTERM via the
  FastAPI lifespan + uvicorn's shutdown budget — no mid-segment connection drops,
  verified against a *container* (PID 1) and not just a test.

### Caching
- [ ] Immutable media (init + segments) served with a long-lived
  `Cache-Control: max-age=…, immutable` and a stable `ETag`; a conditional
  `If-None-Match` gets `304`. VOD playlists are cacheable too.
- [ ] Generated init/segments are **memoized** (cut once → reuse) rather than
  re-muxed per request — the same request yields the same bytes and the same `ETag`,
  the cache is **bounded**, and two concurrent requests for the same cold segment
  cut it once rather than N times.

### Security / abuse protection
- [ ] **Path traversal is impossible:** an `asset`/`rendition`/segment index can
  never escape `MEDIA_DIR` (`../`, absolute paths, symlinks); an unknown asset is a
  clean `404`, not a filesystem probe or a 500.
- [ ] Inputs are **validated & bounded**: the `Range` header syntax, the segment
  index (reject out-of-range), and rendition/asset names — a malformed request is a
  `400`/`404`/`422`, never a `500`.
- [ ] **(Stretch) signed/expiring URLs** or a token gate on playlists — a taste of
  CDN access control. Note the DRM/at-rest boundary you are explicitly *not* doing.

### Observability
- [ ] A structured span/log line per request (via `common_telemetry`) carrying
  `asset`, `rendition`, and — for media — the byte range served. Never log media bytes.
- [ ] Counters: playlists served (master/media/mpd), init & segment requests, **range
  vs full** responses, `416`s, and segment cache **hit/miss**.
- [ ] Histograms: **segment-generation time** (cold cut) and segment size; a gauge for
  assets/renditions loaded.

### Python discipline
- [ ] **pyright strict passes clean** — every `# type: ignore` carries a justifying
  comment.
- [ ] **No blocking call on the event loop** — runs clean under
  `PYTHONASYNCIODEBUG=1`; demuxing, muxing and every `mmap` page fault happen in a
  thread pool deliberately, not by accident.
- [ ] **Bounded pool sized on purpose** — the thread-pool width and uvicorn's worker
  count are tuned *together* against the GIL, with the reasoning in the design doc.
  Muxing is CPU-bound, so more threads past a point buys contention, not throughput.
- [ ] **Profile committed** — a `py-spy` flamegraph and a `memray` run in
  `docs/11-benchmarks.md`, naming the top bottleneck in the mux path and the real
  memory cost of the per-frame sample table.

---

## Cross-cutting scale skills
- **Bounded memory:** segment-at-a-time muxing over an `mmap`'d source, plus chunked
  range serving, keeps RSS independent of asset size — a 4 GB movie packages in a
  segment's worth of RAM.
- **Just-in-time vs pre-packaged:** cut segments on demand and **memoize** them — the
  latency/storage tradeoff every real packager makes.
- **Determinism as a caching contract:** the same source yields **byte-identical**
  init/segments, so an `ETag` and any cache in front stay coherent.
- **Backpressure:** a slow client pulls range bytes at its own rate; you never buffer
  a whole asset to feed it.
- **Knowing where the interpreter stops:** this is the project where CPython's
  ceiling is closest. Finding it, naming the cause, and writing it down is worth more
  than routing around it.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains numbers: **segment-generation throughput** (segments/s and
   MB/s), **first-byte latency** for a cold vs. memoized segment, and a **real player
   playing through** — `ffmpeg`/`hls.js` pulls the master, plays start → `ENDLIST`,
   and performs a **rendition switch** — recorded in `docs/11-benchmarks.md`.
3. A **profile** sits beside those numbers: a `py-spy` flamegraph taken under load
   and a `memray` run, naming the top bottleneck. Numbers alone don't close this —
   you have to know *why* they are what they are. Where CPython cannot reach a target
   in (2), the gap and its cause (GIL contention, GC pressure, allocation in the mux
   loop, a blocking call on the loop) **is** the finding, and it is recorded, not
   scaled away.
4. `docs/11-design.md` records the decisions the SPEC grades: the **box layout** you
   emit (init + `moof`/`mdat`), the **keyframe-aligned segmentation** rule and
   target-duration policy, the **HLS↔DASH mapping**, the **byte-range + ABR-alignment**
   design, and the **memoization/caching** model.
5. `make verify` is green — `ruff format --check` → `ruff check` → `pyright`
   (strict) → `pytest` — and no `raise NotImplementedError` remains on a checked path.

## Suggested order of attack
1. Get the boring path working: `make fixture` generates a two-rendition asset,
   `Catalog.load` scans `MEDIA_DIR`, and `GET /healthz` + `GET /assets` list the
   library — no packaging yet. (All of this is already wired; run it and see.)
2. Build V1: parse the box tree and the `stbl` sample tables into one flat per-sample
   list; unit-test frame count, duration, and keyframe positions against a committed
   fixture. Write the Hypothesis property test *early* — it is much cheaper to keep a
   parser total than to make it total later.
3. Build V2: emit the init segment, then cut **one** keyframe-aligned media segment;
   `make validate` feeds `init + seg` to `ffprobe`.
4. Build V3: generate the HLS media playlist from the segment list, then the master
   and the DASH MPD; `make playlist` shows both; validate them.
5. Build V4: add `Range` → `206`/`416` serving (`make range` shows all four cases),
   then memoize cut segments; check that the second rendition's boundaries align so
   ABR switching is seamless.
6. Add CORS + cache/ETag headers + traversal guards + metrics; point `hls.js`/`ffmpeg`
   at it, benchmark, profile, and document.

## Run it
```bash
make setup && make sync       # .env from .env.example, then uv sync
make fixture                  # generate media/bbb/{720p,1080p}.mp4 (needs ffmpeg)
#   or drop your own at $MEDIA_DIR/<asset>/<rendition>.mp4
make run                      # uv run vod-streaming
#   The scaffold starts and serves. `GET /healthz`, `GET /assets` and `GET /metrics`
#   work; the first playlist/segment request raises NotImplementedError naming the
#   vertical it needs — that message is the worklist.

make assets                   # what the library scan found
make playlist                 # V3 made visible
make range                    # V4 made visible (206 / 416 / Content-Range)
make validate                 # V2's Proof: init + seg through ffprobe
make docker                   # the container: uvloop + PID-1 signal handling

# Once V1–V3 are done, point a player at the master playlist:
ffplay  http://localhost:8080/vod/bbb/master.m3u8
#   or load it in a <video> tag with hls.js — `make frontend` serves the web/ playground.
```
