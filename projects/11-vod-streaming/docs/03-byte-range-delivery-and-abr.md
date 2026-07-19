# Byte-Range Delivery & the ABR Ladder — From First Principles

> A beginner-friendly guide. **No prior knowledge assumed** beyond
> [doc 01 (segments)](./01-fragmented-mp4-segmenter.md) and
> [doc 02 (manifests)](./02-manifests-hls-and-dash.md).
> This teaches the *idea* behind **V4** so you can write the delivery layer yourself.
> It prepares you for [`src/delivery.rs`](../src/delivery.rs) — the `resolve_range()`
> and `serve_ranged()` `todo!()`s — plus the ABR alignment work in the segmenter, and
> the V4 checklist in [`SPEC.md`](../SPEC.md). It teaches the HTTP `Range` state
> machine and the ABR alignment *constraint*; the parser body is yours.

---

## The one sentence to hold onto

**Two problems, two mechanisms: HTTP `Range` lets a client fetch *part* of a resource
(seeking); an ABR ladder of time-aligned, keyframe-started renditions lets the
*client* swap quality mid-stream — and the server stays dumb and cacheable through
both.**

---

## 1. Two realities video delivery must survive

- **Players seek.** A viewer drags to 0:42. The player doesn't want the whole file — it
  wants the *bytes around 0:42*. It needs to ask for a **slice** of a resource.
- **Networks fluctuate.** A stream that's crisp on wifi hits an elevator and the
  bandwidth collapses. Quality must **drop** — without re-buffering from scratch, and
  without the server tracking each viewer.

The first is solved by HTTP `Range`. The second, the deeper one, by **ABR** (adaptive
bitrate). Both are designed so the server does *almost nothing* — all the intelligence
sits in the client. That's what makes internet video scale to a CDN.

---

## 2. HTTP `Range`: fetching part of a resource

Normally `GET /file` returns the whole thing with `200 OK`. But a client can ask for a
byte slice with a `Range` header, and a server that supports it answers `206 Partial
Content`:

```
Client:  GET /vod/bbb/1080p/seg/7
         Range: bytes=500000-999999

Server:  206 Partial Content
         Content-Range: bytes 500000-999999/1500000     (slice / total length)
         Content-Length: 500000                          (bytes IN THIS response)
         Accept-Ranges: bytes
         <500000 bytes>
```

`Range` uses **inclusive** bounds, and there are four forms your `resolve_range()` must
handle. Trace each against a resource of `total = 1000` bytes (offsets 0..=999):

| Request | Meaning | Resolves to |
|---------|---------|-------------|
| *(no header)* | whole resource | **Full** → `200`, bytes 0..=999 |
| `bytes=0-499` | first 500 bytes | **Partial{0, 499}** → `206` |
| `bytes=500-` | offset 500 to EOF (open-ended) | **Partial{500, 999}** |
| `bytes=-200` | the *last* 200 bytes (suffix) | **Partial{800, 999}** |
| `bytes=900-100000` | end past EOF | clamp end → **Partial{900, 999}** |
| `bytes=5000-6000` | start past EOF | **Unsatisfiable** → `416` |

The state machine, as a diagram:

```
Range header?
 ├── none / "bytes=0-"           → Full            → 200, whole body
 ├── "bytes=a-b"  (a ≤ b, a<tot) → clamp b to tot-1, Partial{a,b} → 206
 ├── "bytes=a-"                  → Partial{a, tot-1}              → 206
 ├── "bytes=-n"                  → Partial{tot-n, tot-1}          → 206  (clamp n≤tot)
 └── a ≥ total  OR  reversed     → Unsatisfiable                  → 416
```

The `416` response is specific: status `416 Range Not Satisfiable`, header
`Content-Range: bytes */<total>` (note the `*`), empty body. It tells the client "your
range made no sense; here's how long the resource actually is, try again." The scaffold
models the three outcomes as the [`Resolved`](../src/delivery.rs) enum
(`Full` / `Partial{start,end}` / `Unsatisfiable`) — `resolve_range()` returns one; and
`serve_ranged()` turns it into the status + `Content-Range`/`Content-Length` +
`Accept-Ranges` + the right body slice.

**The bytes-out contract:** on `206`, `Content-Length` = `end - start + 1` (the slice),
and the body is *exactly* those bytes. Slicing `Bytes` is O(1) and shares the buffer —
so a range costs memory bounded by a chunk, not by re-reading or copying the segment.
The scaffold notes this: *`Bytes::slice` keeps a single segment's worth, which is the
bound that matters.*

> **Ambiguous inputs are yours to decide (and document).** Multi-range
> (`bytes=0-9,20-29`), a malformed header, `total == 0` — the scaffold explicitly says
> *pick a simple correct behavior and note it* (e.g. `Unsatisfiable` for the empty
> resource, `Full` or `400` for multi-range). Deciding and writing it down *is* the
> exercise; there's no single "right" answer the SPEC hides from you.

You built the same `206`/`416` machinery in **project 06** (object store). Here it runs
under a *real player's* seek behavior — same mechanism, higher stakes.

---

## 3. Why byte-range serving is the quiet foundation of *everything*

Range serving isn't just for seeking. It's *also* why single-file packaging works at
all: a player can pull `init.mp4`'s bytes, then a segment's bytes, then a different
byte-window on a seek — all from plain files over plain HTTP, with a CDN caching each
range. No special media server, no streaming protocol, no per-connection state. `GET`
with a `Range` header *is* the streaming protocol. Internalizing that collapses a lot of
mystique: "video streaming" is mostly disciplined HTTP.

---

## 4. ABR: the client adapts, the server stays dumb

Now the deep half. You published the same content at several bitrates (the ladder in
doc 02's master playlist). **Adaptive bitrate** is the player, after each segment fetch,
measuring how fast that segment downloaded and deciding which rung to fetch next:

```
Player downloads 1080p seg 3 → took 9s for a 6s segment → too slow, I'm draining buffer
   → next: fetch 720p seg 4 instead
Player downloads 720p seg 4 → took 2s for a 6s segment → plenty of headroom
   → next: climb back to 1080p seg 5
```

Everything adaptive lives **client-side**. The server just serves whatever segment URL
is asked for, as cacheable HTTP. What that buys you operationally is the whole point:

- **No per-viewer server state** — the server doesn't know or care who's on which rung.
- **CDN-friendly** — every segment is a static, cacheable object; one cached `720p/seg/4`
  serves a million viewers.
- **Dumb, horizontally-scalable origin** — it reads files and slices bytes.

> "Why is my video suddenly 240p?" is not a failure — it's the client-side estimator
> doing its job, protecting you from a stall.

---

## 5. The alignment constraint: why ABR needs aligned, keyframe-started segments

Here's the constraint that ties V4 back to V2, and the concept card's **trap**: you
*cannot* test ABR with one rendition. The ladder's entire value — and its one hard rule —
only exists at ≥2 rungs.

For a mid-stream switch to be **seamless** (no gap, no overlap, no glitch), the
renditions must have **time-aligned segment boundaries**, and each segment must start on
its own keyframe (doc 01). Trace a 720p→1080p switch:

**Aligned (correct):**
```
time:   0s      6s      12s      18s
720p:   |--s0--|--s1--|--s2--|--s3--|
1080p:  |--S0--|--S1--|--S2--|--S3--|
                       ↑ switch here: play 720p s0,s1, then 1080p S2,S3
                         S2 starts exactly where s1 ended, AND on a keyframe
                         → seamless: no missing frames, no duplicated frames
```

**Misaligned (broken):**
```
time:   0s      6s      12s        18s
720p:   |--s0--|--s1--|--s2--|
1080p:  |---S0---|---S1---|---S2---|      (boundaries at 0, 7, 14, 21)
                       ↑ after 720p s1 (ends at 12s), 1080p S1 covers 7–14s
                         → 7–12s replayed (overlap) OR 12–14s gap. Glitch either way.
```

So the alignment isn't cosmetic — a switch point that isn't a *shared* boundary
produces a visible gap or a stutter. This is why V2's segmentation policy (keyframe-
aligned, deterministic) had to be right: **ABR is the payoff that the keyframe
discipline was paying for all along.** To make renditions align, they must be cut on a
consistent keyframe cadence — which is a property of how the source was encoded, and a
thing V4's "second rendition" work has to ensure.

> **Depth probe:** why do misaligned renditions cause a gap *or* an overlap
> specifically? Because the player finishes rendition A's segment at time T, and asks
> rendition B for "the segment covering T onward" — if B has no boundary at T, it either
> re-sends frames before T (overlap) or skips to its next boundary after T (gap). Only a
> *shared* boundary at T makes the handoff exact.

---

## 6. Determinism is the caching contract

One more idea the SPEC keeps returning to. Because segments are cut just-in-time and
memoized (not pre-stored), the *same source must always produce byte-identical
segments*. Why it's load-bearing:

- A stable `ETag` (a hash of the bytes) only works if the bytes never change.
- A `Cache-Control: immutable` promise is a lie if a re-mux yields different bytes.
- A CDN caching `720p/seg/4` assumes every origin that ever cuts it agrees to the last
  byte — otherwise caches *silently diverge* and clients get corrupt joins.

That's why doc 01 insisted the init (and every segment) be deterministic: no
timestamps-of-day, no random ids. Determinism → stable `ETag` → coherent cache. It's the
same discipline the horizontal caching checklist rewards (`If-None-Match` → `304`).

### Just-in-time vs pre-packaged (the trade you're making)

You *could* pre-cut every segment of every rendition to disk at startup. Or cut
on-demand and **memoize** (the scaffold's caching TODO — cut once, reuse). The trade:

| | Pre-package everything | JIT + memoize (this project) |
|---|---|---|
| Storage | N renditions × full asset, upfront | just the source + hot segments |
| First request latency | instant (already cut) | one cold cut, then cached |
| Cold-vs-warm gap | none | the metric V4 asks you to measure |

Real packagers pick per workload; here you cut on demand and memoize, and you *measure*
the cold-cut vs memoized first-byte latency (a Definition-of-done bench number).

---

## Mental model summary

| Thing | One-liner |
|-------|-----------|
| `Range` → `206` | serve a byte slice; `Content-Range: a-b/total`, `Content-Length` = slice |
| `416` | unsatisfiable range; `Content-Range: bytes */total`, empty body |
| range forms | `a-b`, `a-` (open), `-n` (suffix); clamp end to `total-1`, `n` to `total` |
| range as foundation | seeking *and* single-file packaging both ride on it |
| ABR | client measures throughput, picks the next rung; server stays dumb |
| server dumbness | no per-viewer state → CDN-cacheable, horizontally scalable |
| alignment constraint | switches only seamless at *shared*, keyframe-started boundaries |
| determinism | same source → byte-identical → stable ETag → coherent cache |
| JIT + memoize | cut once, reuse; measure cold vs warm first-byte latency |

## Where you'll build this

[`src/delivery.rs`](../src/delivery.rs):
- `resolve_range()` — parse `bytes=` (`a-b`/`a-`/`-n`) against `total` → `Resolved` (§2).
- `serve_ranged()` — assemble `200`/`206`/`416` with the right headers + body slice.

Plus the ABR half: a **second rendition** whose segment boundaries **align** with the
first (in the segmenter + master playlist), and the caching/`ETag` horizontal work
(memoize cut segments — see the TODO in [`catalog.rs`](../src/catalog.rs)).

**This doc unlocks these V4 "Done when ALL true" boxes:** `206`+`Content-Range` for a
range, `200` for none; open-ended/suffix ranges + `416` for unsatisfiable;
`Accept-Ranges` + matching `Content-Length`; a real ≥2-rung ladder with time-aligned
boundaries; a streamed (not fully-buffered) body.

**The interesting parts are yours:** the exact `Range` parser (and your documented
choice for the ambiguous inputs), and how you guarantee two renditions align. Record the
byte-range + ABR-alignment design in `docs/11-design.md`. For a nudge use
[`/hint`](../../..); for a guided, test-first run at V4 use [`/quest`](../../..).
