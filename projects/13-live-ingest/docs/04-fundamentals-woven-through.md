# The Backend Fundamentals Woven Through This Project

> The four verticals are the headline acts; this doc covers the fundamentals the
> [SPEC](../SPEC.md)'s **horizontal checklist** and **cross-cutting scale
> skills** weave between them: fan-out and the cache-header ladder, bounding a
> hostile publisher, graceful stream-end, and the observability that makes a
> latency product debuggable. No prior knowledge assumed. Read it once before
> starting V1, then again before the 🐉 boss fight — every section here is a
> boss-fight criterion in street clothes.
>
> Anchored to the wired plumbing: [live.py](../src/live_ingest/live.py) (window, registry,
> fan-out), [routes.py](../src/live_ingest/routes.py) (headers, CORS, path guards),
> [ingest.py](../src/live_ingest/ingest.py) / [main.py](../src/live_ingest/main.py) (lifecycle,
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
                                                              │ (bytes, by reference)
                        200 viewers ◀── HTTP GETs ◀───────────┘
```

The naive version re-does work per viewer — render the playlist *and re-mux the
part* inside each GET handler. At 200 viewers × 5 parts/s that's 1,000 muxes/s
of identical output. The wired design does it once:
[`Fragmenter.cut_part`](../src/live_ingest/fmp4.py) runs on the *publisher's* session
task, its output lands in [`LiveStream`](../src/live_ingest/live.py) as `bytes`, and every
viewer's [`part_bytes`](../src/live_ingest/live.py) is a refcount bump — handing out an
immutable `bytes` object is a pointer copy, not a memcpy. This is the horizontal "memoized in the live
window" box, and the boss fight checks it with a counter: **each part muxed
once**, proven by instrumentation, not asserted.

The same one-to-many economy drives the blocking-reload design (N held requests
parked on one `asyncio.Event` — see
[03-llhls-blocking-reload.md](03-llhls-blocking-reload.md) §4) and the next
section's headers: a CDN is just fan-out you don't have to serve yourself.

## 2. The cache-header ladder: TTL = lifetime

HTTP caching has one honest rule: **tell the cache the truth about how long the
bytes stay valid.** This project is unusual in spanning the whole spectrum in
four routes — already wired in [routes.py](../src/live_ingest/routes.py), graded by the
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
`CORSMiddleware(allow_origins=["*"])` lets a browser player on another origin fetch at all
(its TODO says tighten it; "which origins may embed my streams" is a real
policy decision, not boilerplate).

## 3. Bounding the hostile publisher (and the greedy viewer)

Port 1935 takes bytes from strangers *before any auth*. The verticals each
check their own lengths (V1 §6, V2 §2.2); this is the systemic view the
horizontal security section grades — every number the peer controls, and the
cap that answers it:

| the peer controls | unbounded cost | the cap | enforced in |
| --- | --- | --- | --- |
| declared message length | GB-scale allocation | `max_message_size` (16 MB, `MAX_MESSAGE_SIZE` in [`rtmp.py`](../src/live_ingest/rtmp.py)) checked *before* extending `partial` | your V1 |
| chunk size | one chunk swallows the stream | clamp in [`set_chunk_size`](../src/live_ingest/rtmp.py) | wired |
| AMF string/object lengths, nesting depth | short slice read as a number / huge alloc / `RecursionError` | bounds-check against remaining bytes; cap depth | your V2 |
| stream key | broadcast as anyone | [`authorize`](../src/live_ingest/live.py) gate; raw key never logged | wired + your V2 |
| session duration × bitrate | RAM ∝ airtime | fixed ring, `window_segments` | wired ([`push_part`](../src/live_ingest/live.py)'s trim) |
| number of publishers | task/memory exhaustion | a concurrent-publisher cap | **yours to add** ([`RtmpIngest`](../src/live_ingest/ingest.py) currently accepts unboundedly) |
| `_HLS_msn` far ahead | parked connections forever | `MAX_BLOCK_SECONDS` / reject | wired shell, your V4 policy |
| URL path segments | probe outside the store | [`safe_key`](../src/live_ingest/routes.py) (empty/`.`/`..`/NUL/`\` ⇒ 400) | wired |

The failure-domain rule ties the table together: **a violation kills that
session, never the server** — visible in the wired
[`RtmpIngest`](../src/live_ingest/ingest.py) connection handler, where any `ProtocolError`
ends *this* connection's task and nothing else. The same idea is the SPEC's backpressure
skill: bounded read buffers and a capped window mean a stalled or bursty
broadcaster degrades *its own* stream — its socket fills, TCP pushes back on
*its* uplink — while other sessions' tasks never notice. Isolation by
bounded-everything, not by heroics. In this build the bounded read buffer is
literal: each publisher's `asyncio.StreamReader` is created with
`limit=RTMP_READ_BUFFER_BYTES`, and once twice that sits unread the transport
stops reading the socket ([ingest.py](../src/live_ingest/ingest.py)).

## 4. Ending well: stream end and graceful shutdown

Live systems are judged at the edges. Two distinct endings, one shared rule —
**finish the sentence before hanging up**:

**The broadcaster leaves** (or its connection drops — same thing on the wire).
Wired in [`PublishSession.run`](../src/live_ingest/session.py)'s `finally`:
[`mark_ended`](../src/live_ingest/live.py) flips the flag your V4 renderer turns into
`#EXT-X-ENDLIST` — the tag that tells players "this is over, stop reloading,
play out and stop" instead of hammering a dead playlist. Your V3/V2 code owes
the other half: finalize the forming segment (`finish_segment`) so the last
seconds are watchable. Note `await_edge`'s stream-ended branch already returns
rather than parking a viewer on an edge that will never advance.

**You are asked to stop** (SIGTERM — every deploy, every autoscale-down). The
wiring in [main.py](../src/live_ingest/main.py): uvicorn stops *accepting* while in-flight
requests — including held blocking reloads, which is why their `MAX_BLOCK_SECONDS` bound
matters here too — drain; then the lifespan stops the RTMP server and cancels
its sessions. That order is backwards for viewers, and the TODO on
`RtmpIngest.stop` marks your remaining piece: on shutdown, live publishers' streams should end
*as if the broadcaster left* — ENDLIST, finalized segment, drained holds — so a
deploy looks to viewers like a broadcast ending, not a `curl: connection reset`.
This is the horizontal "graceful shutdown / stream end" box.

## 5. Observability: watching latency creep before a viewer does

This project's product *is* a latency number, so the metrics are latency-shaped.
The checklist asks for three layers (telemetry init is wired via
`common_telemetry`; a request id per HTTP request via its `RequestIdMiddleware`
in [main.py](../src/live_ingest/main.py), and an `rtmp_session` id bound per connection in
[ingest.py](../src/live_ingest/ingest.py) — the hashed key, the msn/part context and all
counters are yours to add as you build; the series are declared in
[metrics.py](../src/live_ingest/metrics.py)):

- **Log contexts** — one per RTMP session (session id + *hashed* stream key — §3's
  never-log-the-credential rule, prefigured by `PublishSession.stream_key`'s docstring), one
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
`MAX_BLOCK_SECONDS`) is a latency knob, so the boss fight measures latency as a
*distribution over time* — sustained p99, not a lucky first minute — and these
are the instruments that let you see it the way the fight will.

---

## 6. Mental model summary

| Fundamental | Hold onto |
| --- | --- |
| Fan-out | mux once per part on the publisher's task; viewers get `bytes` refcounts; prove "once" with a counter |
| Cache ladder | TTL = true lifetime: `no-store` playlist · 5 s parts · immutable segments+init — made honest by msn monotonicity and byte-stable init |
| Content types / CORS | players dispatch on MIME; cross-origin playback needs deliberate CORS, not a permanent `allow_origins=["*"]` |
| Hostile input | every peer-controlled number has a cap; violation kills that session only |
| Backpressure | bounded buffers + capped window ⇒ a slow publisher throttles itself via its own TCP socket |
| Stream end | ENDLIST + finalized last segment; publisher-drop and SIGTERM should look identical to a viewer |
| Graceful shutdown | stop accepting, drain in-flight (bounded holds make draining bounded), then stop |
| Observability | log context per session & request (hashed key); held/served/timed-out counters; **live-edge age** is the alertable glass-to-glass proxy |

## 7. Where these land

No single module — that's the point. The wired halves live in
[routes.py](../src/live_ingest/routes.py), [live.py](../src/live_ingest/live.py),
[session.py](../src/live_ingest/session.py), [ingest.py](../src/live_ingest/ingest.py), and
[main.py](../src/live_ingest/main.py); your halves
arrive *inside* V1–V4 (length checks, the auth call, ENDLIST rendering, the
publisher cap, log context and counters) rather than after them — retrofitting bounds
onto a parser is far harder than parsing defensively from the first byte.

This doc unlocks the horizontal checklist's **Protocols / Caching / Security /
Observability** boxes and stands behind four of the five boss-fight criteria
(latency sustained, bounded memory, fan-out holds, blocking-reload rates) in
[SPEC.md](../SPEC.md). Proofs are named per box there; the design decisions go
in `docs/13-design.md`, the boss numbers in `docs/13-benchmarks.md`.
