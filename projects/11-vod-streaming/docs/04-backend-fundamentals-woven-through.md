# Backend Fundamentals Woven Through a Media Server — From First Principles

> A beginner-friendly guide to the **horizontal** concerns that surround the four
> verticals: content types, immutable caching + ETags, path-traversal safety, CORS for
> browser players, graceful shutdown, and the metrics that matter. **No prior knowledge
> assumed.** This prepares you for the horizontal checklist in [`SPEC.md`](../SPEC.md)
> and the rapid-fire round in [`CONCEPTS.md`](../CONCEPTS.md). It's anchored to
> [`routes.rs`](../src/routes.rs), [`delivery.rs`](../src/delivery.rs),
> [`catalog.rs`](../src/catalog.rs), and [`main.rs`](../src/main.rs). These are the
> things that make a *correct* packager into a *production* one.

---

## The one sentence to hold onto

**A media server's hard parts (V1–V4) sit inside a shell of unglamorous
correctness — the right `Content-Type`, an honest `ETag`, a jail around the
filesystem, a clean shutdown, and metrics you can debug from — and that shell is what
separates "it decodes on my laptop" from "it survives a CDN and the public internet."**

---

## 1. Content types: the bytes are right, but does the player know what they are?

An HTTP response is just bytes plus a `Content-Type` label. Players (and browsers)
dispatch on that label — hand them the correct segment bytes under the *wrong* type and
they refuse to parse it. There's no sniffing to fall back on for these formats. The four
that matter (already defined as constants in [`routes.rs`](../src/routes.rs)):

| Resource | `Content-Type` | Why |
|----------|----------------|-----|
| HLS playlist (`.m3u8`) | `application/vnd.apple.mpegurl` | the registered HLS type; players key on it |
| DASH manifest (`.mpd`) | `application/dash+xml` | the registered DASH type |
| init segment (`init.mp4`) | `video/mp4` | it *is* an mp4 (ftyp+moov) |
| media segment (`seg/n`) | `video/iso.segment` (or `video/mp4`) | a CMAF fragment |

The lesson generalizes: **the label is part of the contract.** Correct bytes under a
wrong or missing type is a silent failure that looks like "the player is broken."

---

## 2. Immutable caching + ETags: media is the *perfect* cache workload

Think about what a VOD segment *is*: once cut, `720p/seg/4` for a given asset **never
changes**. That's the dream case for HTTP caching. Two headers express it:

```
Cache-Control: max-age=31536000, immutable    ← "cache for a year; never revalidate"
ETag: "a1b2c3…"                                ← a stable fingerprint of the bytes
```

`immutable` tells browsers/CDNs "don't even bother checking — this will never change."
That's *only* safe because of doc 01/03's determinism: the same source always yields the
same bytes, so caching them forever can't go stale.

**The conditional-request dance** (bandwidth saving for the client):

```
1st request:  GET /seg/4                     → 200, ETag: "abc", <bytes>
              (client caches bytes + the ETag)
later:        GET /seg/4
              If-None-Match: "abc"           → 304 Not Modified, NO body
              (client reuses its cached copy; you saved sending the whole segment)
```

An `ETag` is just a fingerprint — for deterministic bytes, a hash of them works, or any
stable id derived from `(asset, rendition, index)`. The V4/horizontal criterion ties
these together: *the same request yields the same bytes **and** the same `ETag`*, and
an `If-None-Match` match returns `304`. Playlists are cacheable too (VOD only — doc 02
§5).

### Memoization is the server-side twin

The [`catalog.rs`](../src/catalog.rs) caching TODO is the *other* half: don't re-demux
and re-mux a segment on every request. **Cut once, memoize the bytes** (keyed by
asset/rendition/index), reuse thereafter. This isn't only speed — it's what makes the
`ETag` *stable* (you're literally handing back the same buffer) and lets you measure the
cold-cut-vs-memoized latency the bench asks for. Determinism, memoization, and the
`ETag` are three views of one property.

---

## 3. Path traversal: the filesystem *is* the database, so jail it

This project has no DB — `MEDIA_DIR/<asset>/<rendition>.mp4` on disk *is* the source of
truth (see the `Catalog::load` scan in [`catalog.rs`](../src/catalog.rs)). That makes
**path traversal** the marquee security risk: a request naming an asset/rendition
becomes a filesystem path, and an attacker will try to escape the media directory.

```
GET /vod/../../etc/passwd/master.m3u8
GET /vod/%2e%2e%2f%2e%2e%2fetc/passwd/...       (URL-encoded ../)
GET /vod/bbb/..%2f..%2fsecret/index.m3u8
              a symlink inside MEDIA_DIR pointing at /etc
```

Every one of these must be **impossible** — not "usually caught," impossible. The safe
posture is *allow-list, don't block-list*:

- Don't try to strip `../`. Instead, **validate names against a strict pattern** (e.g.
  a known charset, no separators, no `.`), *then* look them up in the in-memory catalog
  you scanned at startup. An unknown asset is a clean **`404`**, not a filesystem probe.
- After joining paths, **canonicalize and verify the result is still inside
  `MEDIA_DIR`** — this is what defeats symlinks and encoded escapes.
- The segment *index* is an input too: reject a non-numeric or out-of-range `seg/{n}`
  with `400`/`404`, never a panic. (The scaffold has `AppError::SegmentOutOfRange` and
  `InvalidRequest` waiting for exactly this.)

The principle: **untrusted input becomes a lookup key, never a raw path.** An unknown
input is an ordinary `404` — it must be indistinguishable from "that asset doesn't
exist," so you leak nothing about the filesystem.

---

## 4. CORS: browser players fetch from another origin

A browser video player (`hls.js`, `dash.js`) usually runs on `myapp.com` but fetches
segments from your media server on another origin. The browser's **same-origin policy**
blocks that by default; **CORS** headers are how your server opts in.

The twist specific to a media server: it's not enough to *allow* the request — for
byte-range reads (doc 03) to work cross-origin, the browser must be allowed to *read the
range headers back*. That's `Access-Control-Expose-Headers`:

```
Access-Control-Allow-Origin: https://myapp.com
Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges
```

Without the `Expose-Headers` line, the fetch *succeeds* but JavaScript can't see
`Content-Range` — so the player can't tell what slice it got, and range logic **fails
silently.** The scaffold's [`routes.rs`](../src/routes.rs) currently uses
`CorsLayer::permissive()` with a `TODO(horizontal)` to tighten it *and* add the expose
list — that expose list is the media-specific gotcha this checklist item is testing.

---

## 5. Graceful shutdown: don't cut a viewer off mid-segment

When the server gets `SIGTERM` (a deploy, a scale-down), the naive thing is to drop
every connection immediately. For a media server that's a *visible* error: a viewer
streaming `seg/40` gets a truncated segment and the player throws. **Graceful shutdown**
means: stop accepting new connections, but **let in-flight segment streams finish
draining** before exiting. `main.rs` already wires an axum shutdown signal; the concept
to own is *why* — a mid-segment cut isn't a silent server metric, it's a glitch the user
sees. The horizontal criterion is exactly "drains in-flight segment streams on SIGTERM,
no mid-segment connection drops."

---

## 6. Observability: metrics you can actually debug a media server from

`common-telemetry` gives you a `tracing` span per request (wired via
`make_request_span` in [`routes.rs`](../src/routes.rs)). The discipline is to enrich it
with *media-specific* context and to count the things whose ratios reveal problems:

- **Span fields:** `asset`, `rendition`, and — for a media response — the **byte range
  served**. Never log media bytes themselves.
- **Counters (the ratios that matter):**
  - playlists served (master / media / mpd),
  - init & segment requests,
  - **range vs full** responses (a healthy player is mostly `206`s),
  - `416`s (a spike = a client sending bad ranges, or your parser is wrong),
  - segment cache **hit / miss** (low hit ratio = memoization not working).
- **Histograms & gauges:** cold **segment-generation time** (how long a cut takes) and
  segment size; a gauge for assets/renditions loaded.

Why these specifically? Each answers a question you'll actually ask in an incident:
*"is the cache working?"* (hit/miss), *"are clients seeking normally?"* (range vs full),
*"why is first-byte slow?"* (cold generation time). Metrics aren't decoration — they're
the questions you'll need answered when it's 2 a.m. and playback is stuttering. (This
project's `/incident` drill will break something and hand you *only* these signals.)

---

## 7. The stretch: signed URLs, and the boundary you're *not* crossing

The checklist's stretch goal is **signed/expiring URLs** — a token in the playlist/segment
URL that the server validates and that expires, so a leaked link stops working. It's a
taste of real CDN access control (think the `?Expires=…&Signature=…` you've seen on
media URLs). The concept to note honestly: this is *access control*, not *content
protection*. **DRM** (encrypting the media at rest so even a downloaded segment is
useless without a license) is a whole other layer this project explicitly does **not**
do. Knowing where that boundary sits — URL gating vs. encryption — is itself the
learning.

---

## Mental model summary

| Concern | The one thing to hold onto |
|---------|---------------------------|
| Content types | the `Content-Type` label is part of the contract; wrong label = refusal |
| Immutable caching | deterministic bytes → `immutable` + stable `ETag` → `If-None-Match`→`304` |
| Memoization | cut once, reuse; it's what *makes* the `ETag` stable and cold latency measurable |
| Path traversal | untrusted input → lookup key, never a raw path; unknown = plain `404` |
| CORS | allow the origin *and* expose `Content-Range`/`Accept-Ranges`, or ranges fail silently |
| Graceful shutdown | drain in-flight segments on SIGTERM; a mid-segment cut is user-visible |
| Metrics | count range-vs-full, cache hit/miss, `416`s, cold cut time — the incident questions |
| Signed URLs (stretch) | access control ≠ DRM; know the boundary you're not crossing |

## Where you'll build this

Woven across the modules rather than one vertical:
[`routes.rs`](../src/routes.rs) (content types, CORS, spans),
[`delivery.rs`](../src/delivery.rs) (`Cache-Control`/`ETag` on media responses),
[`catalog.rs`](../src/catalog.rs) (memoization; the `MEDIA_DIR` scan + name validation),
and [`main.rs`](../src/main.rs) (graceful shutdown, telemetry init).

**This doc unlocks the horizontal checklist boxes:** correct content types;
`Range`→`206`/`416` with `Accept-Ranges`; CORS with exposed headers; graceful shutdown;
immutable caching + `ETag` + `304`; memoized segments; path-traversal impossible +
bounded/validated inputs; the tracing span, counters, and histograms; and the stretch
signed-URL gate.

**The build is yours** — these are *criteria*, not recipes. For a graduated nudge on any
one use [`/hint`](../../..); for the full round-trip (metrics you then debug with) see the
`/incident` drill referenced in the SPEC.
