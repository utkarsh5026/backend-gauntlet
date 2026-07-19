# The Backend Fundamentals Woven Through This Project

> The four verticals are the headline acts; this doc covers the fundamentals the
> [SPEC](../SPEC.md)'s **horizontal checklist** and **cross-cutting scale
> skills** weave between them: fan-out and the cache-header ladder, bounding a
> hostile publisher, graceful stream-end, and the observability that makes a
> latency product debuggable. No prior knowledge assumed. Read it once before
> starting V1, then again before the 🐉 boss fight — every section here is a
> boss-fight criterion in street clothes.
>
> Anchored to the wired plumbing: [live.rs](../src/live.rs) (window, registry,
> fan-out), [routes.rs](../src/routes.rs) (headers, CORS, path guards),
> [session.rs](../src/session.rs) / [main.rs](../src/main.rs) (lifecycle,
> shutdown), and the bounds you'll enforce inside your V1–V4 code.

---

## 0. The one sentence to hold onto

**One publisher fans out to N viewers, so everything expensive must happen once
per part — built once, cached by lifetime, bounded in memory, ended cleanly —
while everything a stranger controls (bytes, lengths, keys, held requests) must
be range-checked, capped, or timed out before it can hurt anyone but itself.**

---

## 1. Fan-in/fan-out: build once, serve N

The shape of a live system is a funnel glued to a megaphone:

```
 1 publisher ──▶ [V1 parse]──▶[V2 gate]──▶[V3 mux ONCE]──▶ LiveStream window
                                                              │ (Bytes, refcounted)
                        200 viewers ◀── HTTP GETs ◀───────────┘
```

The naive version re-does work per viewer — render the playlist *and re-mux the
part* inside each GET handler. At 200 viewers × 5 parts/s that's 1,000 muxes/s
of identical output. The wired design does it once:
[`Fragmenter::cut_part`](../src/fmp4.rs) runs on the *publisher's* session
task, its output lands in [`LiveStream`](../src/live.rs) as `Bytes`, and every
viewer's [`part_bytes`](../src/live.rs) is a refcount bump — `Bytes::clone` is
a pointer copy, not a memcpy. This is the horizontal "memoized in the live
window" box, and the boss fight checks it with a counter: **each part muxed
once**, proven by instrumentation, not asserted.

The same one-to-many economy drives the blocking-reload design (N held requests
parked on one `watch` signal — see
[03-llhls-blocking-reload.md](03-llhls-blocking-reload.md) §4) and the next
section's headers: a CDN is just fan-out you don't have to serve yourself.

## 2. The cache-header ladder: TTL = lifetime

HTTP caching has one honest rule: **tell the cache the truth about how long the
bytes stay valid.** This project is unusual in spanning the whole spectrum in
four routes — already wired in [routes.rs](../src/routes.rs), graded by the
horizontal checklist:

| resource | changes… | header (wired) | why |
| --- | --- | --- | --- |
| `index.m3u8` | every ~200 ms | `no-store` | any cached copy is stale before the RTT ends; a CDN serving a 1 s-old playlist re-adds the latency V4 just removed |
| `part/{msn}/{part}` | never, but evicted in seconds | `max-age=5` | immutable content with a short *relevance* window — cacheable long enough to absorb a thundering herd, short enough not to waste CDN memory |
| `seg/{msn}` | never (complete) | `max-age=31536000, immutable` | an msn is never reused (`next_msn` is monotonic), so the URL permanently names those bytes — content addressing via URL |
| `init.mp4` | never (byte-stable, V3's contract) | same | fetched once per viewer, identical for the whole broadcast |

Two things make the immutable rows *true* rather than hopeful: msn
monotonicity (never recycle a sequence number into different bytes) and V3's
byte-stable init. Break either and a CDN happily serves the lie forever —
`immutable` is a promise, not a hint. The checklist also asks for a stable
`ETag` on the immutable rows (cheap: the bytes never change); project 06's
[02-how-etags-work.md](../../06-object-store/docs/02-how-etags-work.md) is the
full theory.

**Content types and CORS** ride along in the same file: the playlist is
`application/vnd.apple.mpegurl`, init is `video/mp4`, media is
`video/iso.segment` — players *do* dispatch on these — and
`CorsLayer::permissive()` lets a browser player on another origin fetch at all
(its TODO says tighten it; "which origins may embed my streams" is a real
policy decision, not boilerplate).

## 3. Bounding the hostile publisher (and the greedy viewer)

Port 1935 takes bytes from strangers *before any auth*. The verticals each
check their own lengths (V1 §6, V2 §2.2); this is the systemic view the
horizontal security section grades — every number the peer controls, and the
cap that answers it:

| the peer controls | unbounded cost | the cap | enforced in |
| --- | --- | --- | --- |
| declared message length | GB-scale allocation | `max_message_size` (16 MB, set in [`Session::run`](../src/session.rs)) checked *before* extending `partial` | your V1 |
| chunk size | one chunk swallows the stream | clamp in [`set_chunk_size`](../src/rtmp.rs) | wired |
| AMF string/object lengths | slice past end / huge alloc | bounds-check against remaining bytes | your V2 |
| stream key | broadcast as anyone | [`authorize`](../src/live.rs) gate; raw key never logged | wired + your V2 |
| session duration × bitrate | RAM ∝ airtime | fixed ring, `window_segments` | wired ([`push_part`](../src/live.rs)'s trim) |
| number of publishers | task/memory exhaustion | a concurrent-publisher cap | **yours to add** ([`accept_loop`](../src/session.rs) currently accepts unboundedly) |
| `_HLS_msn` far ahead | parked connections forever | `MAX_BLOCK` / reject | wired shell, your V4 policy |
| URL path segments | probe outside the store | [`guard_name`](../src/routes.rs) (empty/`.`/`..`/NUL/`\` ⇒ 400) | wired |

The failure-domain rule ties the table together: **a violation kills that
session, never the server** — visible in the wired
[`Session::run`](../src/session.rs) loop, where any `Err` breaks *this*
connection's loop and nothing else. The same idea is the SPEC's backpressure
skill: bounded read buffers and a capped window mean a stalled or bursty
broadcaster degrades *its own* stream — its socket fills, TCP pushes back on
*its* uplink — while other sessions' tasks never notice. Isolation by
bounded-everything, not by heroics.

## 4. Ending well: stream end and graceful shutdown

Live systems are judged at the edges. Two distinct endings, one shared rule —
**finish the sentence before hanging up**:

**The broadcaster leaves** (or its connection drops — same thing on the wire).
Wired in [`Session::run`](../src/session.rs)'s epilogue:
[`mark_ended`](../src/live.rs) flips the flag your V4 renderer turns into
`#EXT-X-ENDLIST` — the tag that tells players "this is over, stop reloading,
play out and stop" instead of hammering a dead playlist. Your V3/V2 code owes
the other half: finalize the forming segment (`finish_segment`) so the last
seconds are watchable. Note `await_edge`'s producer-gone branch already returns
rather than parking a viewer on an edge that will never advance.

**You are asked to stop** (SIGTERM — every deploy, every autoscale-down). The
wiring in [main.rs](../src/main.rs): axum's `with_graceful_shutdown` stops
*accepting* while in-flight requests — including held blocking reloads, which
is why their `MAX_BLOCK` bound matters here too — drain; then the `watch`
channel tells the RTMP accept loop to wind down. The `shutdown_signal` TODO
marks your remaining piece: on shutdown, live publishers' streams should end
*as if the broadcaster left* — ENDLIST, finalized segment, drained holds — so a
deploy looks to viewers like a broadcast ending, not a `curl: connection reset`.
This is the horizontal "graceful shutdown / stream end" box.

## 5. Observability: watching latency creep before a viewer does

This project's product *is* a latency number, so the metrics are latency-shaped.
The checklist asks for three layers (telemetry init is wired via
`common_telemetry`; HTTP request spans via the `TraceLayer` in
[routes.rs](../src/routes.rs) — the RTMP-session span and all counters are
yours to add as you build):

- **Spans** — one per RTMP session (session id + *hashed* stream key — §3's
  never-log-the-credential rule, prefigured by `Session`'s field comment), one
  per HTTP request (key + requested msn/part), so "viewer X stalled" and
  "publisher Y misbehaved" are each one trace query.
- **Counters** — publishers connected/rejected, bytes in, segments/parts
  produced, viewer requests by kind, and **blocking reloads held / served /
  timed-out**. That last triple is V4's health in three integers: held≈served
  and timed-out≈0 is the design working; served-without-holding means players
  are behind; timed-out climbing means the publisher or your cut cadence
  stalled. The parts-produced counter is also the boss fight's
  muxed-once proof (§1).
- **Gauges/histograms** — packaging latency per part, active publishers, held
  requests, ingest bitrate, and the queen of them all: **live-edge age** = now −
  newest part's PTS. It's the glass-to-glass proxy you can compute server-side
  with no player cooperation: if the newest part is 250 ms old at a 300 ms part
  target, you're healthy; if it's 3 s old, every viewer is 3 s further behind
  and only this gauge told you *before* the complaints did. Alert on it.

The through-line: every knob in this project (part target, hold-back, window,
`MAX_BLOCK`) is a latency knob, so the boss fight measures latency as a
*distribution over time* — sustained p99, not a lucky first minute — and these
are the instruments that let you see it the way the fight will.

---

## 6. Mental model summary

| Fundamental | Hold onto |
| --- | --- |
| Fan-out | mux once per part on the publisher's task; viewers get `Bytes` refcounts; prove "once" with a counter |
| Cache ladder | TTL = true lifetime: `no-store` playlist · 5 s parts · immutable segments+init — made honest by msn monotonicity and byte-stable init |
| Content types / CORS | players dispatch on MIME; cross-origin playback needs deliberate CORS, not permanent `permissive()` |
| Hostile input | every peer-controlled number has a cap; violation kills that session only |
| Backpressure | bounded buffers + capped window ⇒ a slow publisher throttles itself via its own TCP socket |
| Stream end | ENDLIST + finalized last segment; publisher-drop and SIGTERM should look identical to a viewer |
| Graceful shutdown | stop accepting, drain in-flight (bounded holds make draining bounded), then stop |
| Observability | span per session & request (hashed key); held/served/timed-out counters; **live-edge age** is the alertable glass-to-glass proxy |

## 7. Where these land

No single module — that's the point. The wired halves live in
[routes.rs](../src/routes.rs), [live.rs](../src/live.rs),
[session.rs](../src/session.rs), and [main.rs](../src/main.rs); your halves
arrive *inside* V1–V4 (length checks, the auth call, ENDLIST rendering, the
publisher cap, spans and counters) rather than after them — retrofitting bounds
onto a parser is far harder than parsing defensively from the first byte.

This doc unlocks the horizontal checklist's **Protocols / Caching / Security /
Observability** boxes and stands behind four of the five boss-fight criteria
(latency sustained, bounded memory, fan-out holds, blocking-reload rates) in
[SPEC.md](../SPEC.md). Proofs are named per box there; the design decisions go
in `docs/13-design.md`, the boss numbers in `docs/13-benchmarks.md`.
