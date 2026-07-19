# Manifests: The Stream's Table of Contents — From First Principles

> A beginner-friendly guide. **No prior knowledge assumed** beyond
> [doc 01 (the segments exist now)](./01-fragmented-mp4-segmenter.md).
> This teaches the *idea* behind **V3** so you can write the manifest generators
> yourself. It prepares you for [`src/manifest.rs`](../src/manifest.rs) — the
> `hls_media_playlist()`, `hls_master_playlist()`, and `dash_mpd()` `todo!()`s — and
> the V3 checklist in [`SPEC.md`](../SPEC.md). It teaches the tag/XML *vocabulary* and
> the *arithmetic contract*; the golden-file output is yours to produce.

---

## The one sentence to hold onto

**A manifest is a plain-text index the player reads *before* any media — it turns "a
pile of segments" into "a playable stream" by naming the init, listing every segment
with its *exact* duration, and advertising the rendition ladder — and the player
*trusts its arithmetic completely.***

---

## 1. The problem: segments alone aren't a stream

After V2 you can produce `init.mp4` and `seg/0`, `seg/1`, `seg/2`, …. But hand a
player a directory of those files and it's helpless. It doesn't know:

- How many segments are there? When does it end?
- How long is each one? (It needs this to build a seek bar and to pace fetching.)
- Where's the init segment they all depend on?
- What *renditions* (qualities) exist, and at what bandwidth, so it can choose?

The player needs an **index it reads first**, before fetching a single frame. That
index is the **manifest**. Two dialects dominate:

- **HLS** (Apple): a line-oriented text file, `.m3u8`, made of `#EXT-X-…` tags.
- **DASH** (MPEG): an XML file, `.mpd`, with a `SegmentTemplate`/`SegmentTimeline`
  model.

Different syntax; **same underlying facts** — the same segment list V2 produced,
described twice. That's the theme of V3: *one segment list → two encodings.*

---

## 2. HLS is two levels: master → media

HLS separates "what qualities exist" from "what segments a quality has":

```
master.m3u8                         ← the ABR ladder: which renditions exist
 ├── 1080p/index.m3u8               ← media playlist: 1080p's segment list
 │     init.mp4, seg/0, seg/1, ...
 └── 720p/index.m3u8                ← media playlist: 720p's segment list
       init.mp4, seg/0, seg/1, ...
```

A player's flow: fetch **master** → pick a starting rung → fetch that rung's **media
playlist** → fetch its init + segments → (measure bandwidth, maybe switch rungs — that's
V4). Your routes already mirror this:
`/vod/{asset}/master.m3u8` and `/vod/{asset}/{rendition}/index.m3u8`
(see [`routes.rs`](../src/routes.rs)).

### The media playlist, tag by tag

Here's what a VOD media playlist looks like, and what each required tag *does* — this
is the vocabulary the `hls_media_playlist()` TODO lists:

```
#EXTM3U                         every HLS file starts with this magic line
#EXT-X-VERSION:7                7+ is REQUIRED for fMP4 + EXT-X-MAP
#EXT-X-TARGETDURATION:7         an UPPER BOUND: ceil(longest segment), in seconds
#EXT-X-MEDIA-SEQUENCE:0         index of the first segment listed
#EXT-X-PLAYLIST-TYPE:VOD        this is on-demand (fully known), not live
#EXT-X-MAP:URI="init.mp4"       the init segment every media segment needs
#EXTINF:6.006,                  segment 0's EXACT duration (seconds)
seg/0
#EXTINF:6.006,
seg/1
#EXTINF:5.339,                  the last one is usually shorter
seg/2
#EXT-X-ENDLIST                  VOD: the list is COMPLETE — no more segments coming
```

| Tag | What it controls | What breaks if it's wrong/missing |
|-----|------------------|-----------------------------------|
| `#EXTM3U` | file magic | not recognized as HLS at all |
| `#EXT-X-VERSION:7` | feature level | fMP4/`EXT-X-MAP` unsupported below 7 → refuses |
| `#EXT-X-TARGETDURATION` | *upper bound* on any `#EXTINF` | a segment exceeding it is a spec violation; players may stall |
| `#EXT-X-MAP` | init segment URI | segments can't be initialized → nothing decodes |
| `#EXTINF` | **exact** per-segment duration | seek bar & pacing wrong; drift (§4) |
| `#EXT-X-ENDLIST` | "stream is complete" | player waits forever for more segments (treats it as live) |

Notice `#EXT-X-TARGETDURATION` is `ceil(longest segment)`, **not** the target you fed
the segmenter — because doc 01 §6 means real segments can exceed the target (a long
GOP). This is the concrete downstream reason V2's per-segment durations must be real.

### The master playlist: advertising the ladder

```
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=5200000,RESOLUTION=1920x1080
1080p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2600000,RESOLUTION=1280x720
720p/index.m3u8
```

Each `#EXT-X-STREAM-INF` is one rung: a `BANDWIDTH` (peak bits/sec) and `RESOLUTION`,
followed by that rung's media-playlist URI. This is *the* data ABR needs — the player
reads the menu here and switches rungs against its measured throughput (V4). The
scaffold models a rung as [`RenditionInfo`](../src/manifest.rs) `{ id, bandwidth,
width, height, uri }`; `hls_master_playlist()` renders one `#EXT-X-STREAM-INF` line per
rung.

> **Depth probe:** how does a player pick its *first* rendition with **no** bandwidth
> measurement yet? It can't measure throughput before it's fetched anything — so it
> uses a heuristic (a conservative default rung, or a configured "start low then climb"
> / "start at a middle rung"). This is *why* accurate `BANDWIDTH` values matter even for
> the very first choice.

---

## 3. DASH says the same thing in XML

DASH's `.mpd` expresses identical facts with a different model:

```xml
<MPD type="static" ...>                    static = VOD (vs "dynamic" = live)
  <Period>
    <AdaptationSet ...>                     ~ "the video, in several renditions"
      <Representation bandwidth="5200000" width="1920" height="1080" ...>
        <SegmentList>                       or SegmentTemplate + SegmentTimeline
          <Initialization sourceURL="init.mp4"/>
          <SegmentURL media="seg/0"/>
          <SegmentURL media="seg/1"/>
          ...
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
```

The mapping is direct — memorize this table and DASH stops feeling foreign:

| HLS concept | DASH equivalent |
|-------------|-----------------|
| master playlist | the `MPD` document |
| a rendition (`#EXT-X-STREAM-INF`) | a `<Representation>` |
| the init (`#EXT-X-MAP`) | `<Initialization sourceURL=…>` |
| a segment + `#EXTINF` | `<SegmentURL>` + its duration in the timeline |
| `#EXT-X-PLAYLIST-TYPE:VOD` / `ENDLIST` | `MPD type="static"` |

The `dash_mpd()` TODO asks for exactly this: same segment list, described the DASH way,
*proving you understand both models map onto the one list.*

> **Why does the industry keep both?** HLS is native on Apple (iOS/Safari/tvOS) and is
> most of the web + most live; DASH is common on Android and smart TVs. Since CMAF (doc
> 01) made the *media* identical, a server can serve one set of segments and just emit
> *both* manifests over them. That's the whole reason V3 has you generate both.

---

## 4. The one number that must be exact: duration drift

This is the concept the SPEC grades hardest and the concept card flags as the **trap**.

The manifest's durations are **arithmetic the player trusts** to build the seek bar and
pace its buffer. If you round each `#EXTINF` to look clean, the error **accumulates**.

Worked example (verified). Say segments are 180 frames at 29.97 fps
(30000/1001) — each is genuinely **6.006 s**. You round each to a tidy `6.000`:

```
Per-segment rounding error:  6.006 − 6.000 = 0.006 s
90-minute asset ≈ 5400 s → ≈ 899 segments
Accumulated drift: 0.006 × 899 ≈ 5.4 SECONDS off by the end
```

By minute 90 the player's idea of "where am I" is **5.4 seconds** wrong — seeks land in
the wrong place, and the manifest is, in the card's words, "cumulative lying." The V3
criterion — *summed `#EXTINF` equals total duration within **one frame*** — exists to
force you to carry V2's *real* per-segment durations (`SegmentEntry.seconds(timescale)`),
not re-round the target. Emit enough decimal precision that the frame-level truth
survives.

> **The mental correction:** clean-looking numbers are a *liability* here, not a nicety.
> The manifest isn't a display; it's a contract.

---

## 5. Why VOD manifests are cacheable (and live ones aren't)

A subtle but important distinction the card asks you to own. A **VOD** manifest is
*complete and immutable*: `#EXT-X-ENDLIST` / `type="static"` says "this is the whole
list, forever." So it can be cached aggressively (V4's horizontal caching work). A
**live** manifest (project 13) is *mutable* — segments append over time, so it must be
re-fetched and can't be cached the same way. Same file format, opposite caching
posture, entirely because of mutability. Getting the VOD tags right (`PLAYLIST-TYPE:VOD`
+ `ENDLIST`) is what *declares* your stream cacheable.

---

## 6. Why these are pure functions (and easy to test)

Notice the signatures: `hls_media_playlist(index, track, target) -> String`,
`dash_mpd(index, track) -> String`. **No I/O.** They take the segment list V2 computed
and return a string. That's deliberate: the V3 proof is **golden-file tests** — you
commit the expected `.m3u8`/`.mpd` for the fixture and assert your output matches
byte-for-byte (`renders_hls_media_playlist`, `renders_dash_mpd`), plus a real
conformance validator (Apple `mediastreamvalidator`, a DASH validator) run recorded in
`docs/11-benchmarks.md`. Pure string-building is trivially golden-testable — lean on
that.

---

## Mental model summary

| Thing | One-liner |
|-------|-----------|
| Manifest | the index a player reads *before* any media |
| HLS master | the rendition ladder (`#EXT-X-STREAM-INF` per rung) |
| HLS media playlist | one rung's segment list (`EXT-X-MAP` + `EXTINF` per seg + `ENDLIST`) |
| DASH MPD | the same facts in XML (`Representation` + `SegmentList`) |
| `TARGETDURATION` | `ceil(longest segment)` — an *upper bound*, not the target |
| `#EXTINF` | the *exact* per-segment duration; must be real, never rounded |
| Duration drift | rounding accumulates → seconds off over a long asset (the trap) |
| VOD vs live | `ENDLIST`/`static` = complete = cacheable; live = mutable = not |
| Pure functions | segment list → string; golden-file testable |

## Where you'll build this

[`src/manifest.rs`](../src/manifest.rs):
- `hls_media_playlist()` — the tags + one exact `#EXTINF` per segment + `ENDLIST`.
- `hls_master_playlist()` — one `#EXT-X-STREAM-INF` per `RenditionInfo`.
- `dash_mpd()` — the same segment list as a `static` MPD.

**This doc unlocks these V3 "Done when ALL true" boxes:** a complete HLS media playlist
(`EXTINF`/`EXT-X-MAP`/`TARGETDURATION`/`ENDLIST`); a master playlist with `BANDWIDTH` +
`RESOLUTION` per rendition; a DASH MPD over the same segments; summed `#EXTINF` = total
within one frame; playlists a real validator/player accepts.

**The build is yours:** the exact byte layout of your golden output, how you format
`#EXTINF` precision, and the HLS↔DASH mapping decisions (record them in
`docs/11-design.md`). For a nudge use [`/hint`](../../..); for a guided, test-first run
at V3 use [`/quest`](../../..).
