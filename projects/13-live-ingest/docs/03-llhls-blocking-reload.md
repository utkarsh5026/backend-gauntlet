# How Low-Latency HLS Works — Blocking Reload and the Latency Wall

> A ground-up guide to the delivery half of this project: why classic HLS
> viewers sit 15–30 seconds behind the camera, and how LL-HLS gets under ~2 s
> with three moves — **parts**, **preload hints**, and **blocking playlist
> reload**. No prior knowledge of HLS assumed; project 11's manifest work helps
> but isn't required.
>
> This prepares you for **V4** in [SPEC.md](../SPEC.md) — "Low-Latency HLS
> playlist + blocking delivery" — anchored to [llhls.rs](../src/llhls.rs) (the
> `render_media_playlist` `todo!()`, the wired `media_playlist` wait-policy shell,
> `ReloadParams`, `MAX_BLOCK`), the wired park/signal mechanism in
> [live.rs](../src/live.rs) (`LiveEdge`, `await_edge`), and the HTTP surface in
> [routes.rs](../src/routes.rs). The tag vocabulary is Apple's published spec
> and is taught in full; the renderer and the request→edge mapping are yours.

---

## 0. The one sentence to hold onto

**Classic HLS is stuck at segment-granularity latency because the player can
only discover new media by re-polling the playlist; LL-HLS publishes ~200 ms
parts and lets the player's playlist request *block on the server* until the
next part exists — turning "poll and hope" into "park and be woken," which is
also exactly how you must implement it: many held requests parked on one
edge signal.**

---

## 1. The problem: the latency arithmetic of classic HLS

Classic live HLS, end to end:

1. The packager cuts 6-second segments; a segment appears in the playlist only
   when *complete* — media is, on average, already half a segment old at birth.
2. The player re-fetches the playlist about once per target duration to
   discover it.
3. The player buffers ~3 segments before playing (Apple's long-standing
   default hold-back) so one late segment doesn't stall playback.

Item 3 alone puts the viewer **3 × 6 s = 18 s** behind the glass; items 1–2 add
several more. Hence the SPEC's "~15–30 seconds behind."

Every naive fix fails on its own arithmetic:

| naive fix | why it fails |
| --- | --- |
| Shorter segments (say 1 s) | latency floor drops to ~3 s but never below; keyframe-per-segment inflates bitrate; playlist churn and request rate go up ~6× |
| Poll the playlist faster (every 200 ms) | can't see media that isn't published yet; 200 viewers × 5 polls/s = **1,000 req/s** of mostly "nothing changed"; and racing the publisher means guessing next-segment URLs into **404s** |
| Push protocols (WebSocket/WebRTC) | solves latency, abandons everything HLS bought: plain HTTP, CDN cacheability, native `<video>` support |

The interesting constraint is the last row: the answer has to *stay* HTTP. LL-HLS
is the design that breaks the wall without leaving HTTP.

---

## 2. The three moves of LL-HLS

### Move 1 — publish the segment *while it forms*, as parts

Don't wait for the 2 s segment to finish: publish each ~200–350 ms **part** (one
V3 fragment) the moment it's cut. New media is now discoverable ~10× sooner,
and because a part needn't start on a keyframe, the encoder's keyframe cadence
is untouched — the part that *does* carry one is marked `INDEPENDENT=YES` so a
joining player knows where it may start decoding.

### Move 2 — advertise the part that doesn't exist yet

The playlist's last line hints the *next* part's URI
(`#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part/12/3.m4s"`). A player can issue that
GET *early*; the server holds it and responds the instant the part is cut —
media bytes with effectively zero discovery delay.

### Move 3 — blocking playlist reload (the core trick)

The player asks for the *future*:

```
GET /live/testkey/index.m3u8?_HLS_msn=12&_HLS_part=3
                              └────────┬────────┘
              "don't answer until part 3 of segment 12 EXISTS"
```

If that part already exists, respond immediately. If not, **hold the request
open** — for the ~200 ms until it does — then respond with the playlist that
includes it. The poll-and-404 loop becomes one long-poll round trip per part.
This is why the playlist advertises
`#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES`: it's the server's promise that
this contract is honored.

CONCEPTS.md's named trap lives here, and it's worth engraving: **returning the
current playlist early to an `_HLS_msn` you haven't reached is not a friendly
fallback — it defeats the entire mechanism.** The player asked to be *woken*,
not answered. Answer early with a stale playlist and the player just re-asks
instantly — congratulations, you've rebuilt busy-polling with extra steps. The
SPEC's `blocking_reload_unblocks_on_part` proof is "unblocks *exactly when* the
part is pushed — never stale, never 404."

New latency arithmetic: the player runs at a hold-back of a few parts instead
of three segments — `PART-HOLD-BACK` ≈ 3 × 0.333 s ≈ **1 s** of buffer, plus
delivery — which is where the boss fight's "≤ 3 s glass-to-glass, target ~2 s"
comes from. Same protocol family, ~10× lower latency, still plain HTTP.

---

## 3. The playlist, line by line

Everything above is expressed in m3u8 tags. Here's a valid LL-HLS media
playlist mid-broadcast — msn 11 complete, msn 12 forming, 300 ms parts — mapped
against what the scaffold gives you:

```
#EXTM3U
#EXT-X-VERSION:9                          ← parts need protocol v9+
#EXT-X-TARGETDURATION:2                   ← ceil(max segment secs)
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=1.0,CAN-SKIP-UNTIL=12.0
#EXT-X-PART-INF:PART-TARGET=0.3           ← your IngestConfig.target_part_secs
#EXT-X-MEDIA-SEQUENCE:11                  ← oldest segment still in the window
#EXT-X-MAP:URI="init.mp4"                 ← V3's init segment
#EXT-X-PROGRAM-DATE-TIME:2026-07-14T09:15:07.123Z
#EXT-X-PART:DURATION=0.3,URI="part/11/0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.3,URI="part/11/1.m4s"
   ... parts 2–5 ...
#EXTINF:2.0,                              ← only a COMPLETE segment gets EXTINF
seg/11.m4s
#EXT-X-PART:DURATION=0.3,URI="part/12/0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.3,URI="part/12/1.m4s"
                                          ← msn 12 still forming: no EXTINF yet
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part/12/2.m4s"   ← the part that doesn't exist
```

The renderer walks exactly what [`LiveStream::snapshot()`](../src/live.rs)
returns — the `Vec<Segment>` window (each with `msn`, `parts` carrying
`duration`/`independent`, `complete`, `duration`, `program_date_time`) and the
`ended` flag, which when true appends `#EXT-X-ENDLIST` (see
[04-fundamentals-woven-through.md](04-fundamentals-woven-through.md) on
stream-end semantics). Tag-by-tag anchors:

| tag | fed by | the rule that matters |
| --- | --- | --- |
| `#EXT-X-MEDIA-SEQUENCE` | front of the window (`segments[0].msn`) | counts *forward only* as the window slides — a repeat or a backwards step desyncs every player (`media_sequence_advances`) |
| `#EXT-X-PART` | each `Part` in each `Segment` | `INDEPENDENT=YES` from `Part::independent`; URIs must match the wired routes (`part/{msn}/{part}`) |
| `#EXTINF` + segment URI | only `Segment::complete` | listing a forming segment's URI 404s every player that trusts you |
| `#EXT-X-PRELOAD-HINT` | live edge + 1 | the next part index *at the forming segment* — and after a keyframe cut it's `part/{msn+1}/0`; the hint moving correctly across a segment boundary is a classic bug site |
| `#EXT-X-SERVER-CONTROL` | your config | `PART-HOLD-BACK` (how far back the player should sit) must be ≥ 2× — Apple recommends ~3× — `PART-TARGET`; `CAN-SKIP-UNTIL` advertises delta updates (`_HLS_skip`, stub in `ReloadParams::skip` — legal to leave unsupported at first, then don't advertise it) |

Old segments falling off the front, parts appearing at the back, msn marching
forward: the playlist is a **sliding window over `live.rs`'s ring**, re-rendered
per request, never cached (routes already send `Cache-Control: no-store`).

---

## 4. Server-side: many held requests, one signal

Blocking reload's cost lands on your concurrency design. 200 viewers × 1 held
playlist GET each (plus preload-hint GETs) = hundreds of parked requests that
all wake at the *same instant* — the next `push_part`. Design space:

| design | cost at 200 viewers | verdict |
| --- | --- | --- |
| thread per held request | 200 OS threads × ~MB stack, context-storm on wake | the pattern LL-HLS punishes |
| poll loop per request ("is it there yet?" every 10 ms) | 20,000 lock acquisitions/s of pure waste | busy-polling, again |
| **park each request on a shared signal; publisher broadcasts once** | 200 dormant futures ≈ KBs; one `send` wakes all | the intended shape |

The scaffold wires the third design and it's worth reading as a reference
pattern even though you don't have to build it:
[`LiveStream`](../src/live.rs) keeps a `tokio::sync::watch` channel whose value
is the [`LiveEdge`](../src/live.rs) — the newest `(msn, part)`, ordered
msn-major so "has the stream reached my target?" is one `>=` compare.
`push_part` bumps it (`edge_tx.send`); `await_edge(target)` subscribes and
sleeps until `edge >= target`. An async fn awaiting a `watch` receiver is a
parked future — no thread, no poll loop — and one send wakes every waiter. This
is the same long-poll/park pattern you'll meet again at project 16's edge and
project 21's task polling.

What V4 *does* own is the **policy** wrapped around that mechanism, visible as
the wired shell of [`media_playlist`](../src/llhls.rs):

1. **Mapping** `ReloadParams` → the `LiveEdge` to wait for. Sounds trivial;
   isn't. `_HLS_msn=12` alone (no `_HLS_part`) means what, exactly — first part
   of 12, or all of 12? What does `_HLS_part=3` mean if msn 12 finished at part
   2 and the stream moved to 13/0? The scaffold's `params.part.unwrap_or(0)` is
   a starting *stance*, not the final answer — pin your semantics down and
   document them in `docs/13-design.md`.
2. **Bounding the wait.** A request for `_HLS_msn=999999` must not park
   forever — that's a free connection-exhaustion attack. The scaffold's
   `MAX_BLOCK` (5 s) caps it; the SPEC requires a bounded timeout and a clean
   response. (Apple's spec goes further: an `_HLS_msn` more than a couple ahead
   of the newest should be rejected immediately as a `400` rather than held —
   reject-vs-cap is your call to make and document.) Distinguish the *three*
   futures: slightly ahead ⇒ hold; absurdly ahead ⇒ reject/cap; behind but
   evicted ⇒ that's not a hold at all, it's a `404` (the media-fetch routes
   already behave this way — `part_bytes` returning `None` maps to
   `AppError::NotFound`).
3. **What a timeout returns.** Expiring `MAX_BLOCK` and returning the current
   playlist is legal (the part genuinely never came — encoder stalled); what's
   illegal is returning early *when the part was still on schedule* (§2's trap).

One more piece of the ecosystem worth knowing: LL-HLS strongly prefers
**HTTP/2** — hundreds of held GETs (playlist + preload hints) multiplex over
one TCP connection instead of hundreds of sockets fighting per-host connection
limits. The SPEC's horizontal checklist asks you only to *note* HTTP/2 as the
intended transport; axum behind an h2-terminating proxy is the usual shape.

---

## 5. Seeing it work

The proof loop for this vertical is pleasantly physical:

```bash
cargo run -p live-ingest
ffmpeg -re -i sample.mp4 -c copy -f flv rtmp://localhost:1935/live/testkey

curl -s 'http://localhost:8080/live/testkey/index.m3u8'            # snapshot
time curl -s 'http://localhost:8080/live/testkey/index.m3u8?_HLS_msn=<edge+1>&_HLS_part=0'
#     ^ should take ~one part-duration, then return a playlist CONTAINING that part
```

Then a real player — Safari natively, or hls.js with `lowLatencyMode: true`
(the [web/](../web/) playground) — and watch the request waterfall: playlist
GETs that each take ~200 ms *by design*, returning the moment the edge
advances. Glass-to-glass measurement (burned-in timecode vs. what renders) is
the boss fight's arena.

---

## 6. Mental model summary

| Concept | Hold onto |
| --- | --- |
| The wall | latency ≈ segment duration × player hold-back (3): 6 s segments ⇒ ~18 s + discovery |
| Part | one V3 fragment (~200–350 ms), published at birth; keyframe parts marked `INDEPENDENT=YES` |
| Preload hint | the next part's URI advertised before it exists — request early, receive at cut time |
| Blocking reload | `?_HLS_msn=N&_HLS_part=M` parks until that part exists; **never answer early with stale** |
| New arithmetic | hold-back ≈ 3 × part ≈ 1 s ⇒ ~2–3 s glass-to-glass |
| Playlist | a per-request render of `snapshot()`: rolling msn, parts for forming segments, `EXTINF` only when complete, hint at the edge, `ENDLIST` when ended |
| Concurrency | park N futures on one `watch`-ed `LiveEdge`; one `send` wakes all — no threads, no polling |
| Bounds | slightly-ahead ⇒ hold · absurd ⇒ reject or cap (`MAX_BLOCK`) · evicted ⇒ 404 |
| Transport | HTTP/2 so hundreds of held GETs share one connection |

## 7. Where you'll build this

One `todo!()`, one policy shell, both in [llhls.rs](../src/llhls.rs):

- `render_media_playlist()` — §3, walking `snapshot()`; the scaffold's TODO
  lists the exact tag order.
- `media_playlist()` — wired, but §4's mapping and timeout semantics are yours
  to refine and defend.

Everything it renders exists because your V3 pushed it into
[`LiveStream`](../src/live.rs); everything it serves goes out through the wired
[routes.rs](../src/routes.rs) with the cache headers already argued for in
[04-fundamentals-woven-through.md](04-fundamentals-woven-through.md).

This doc unlocks V4's **Done when ALL true** ([SPEC.md](../SPEC.md)): a valid
LL-HLS playlist · blocking reload that holds and never returns stale · the
playlist advancing every part, monotonic · clean 404s outside the window +
bounded holds · lifetime-appropriate cache headers. Proof:
`playlist_has_llhls_tags`, `media_sequence_advances`,
`blocking_reload_unblocks_on_part`, and a live Safari / hls.js session noted in
`docs/13-benchmarks.md`.
