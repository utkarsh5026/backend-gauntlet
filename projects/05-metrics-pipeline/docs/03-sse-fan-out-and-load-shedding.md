# SSE Fan-out & Load Shedding — The *Other* Backpressure, From First Principles

> How one stream of closed windows reaches N browser tabs without the slowest
> tab stalling the pipeline, what Server-Sent Events actually put on the wire,
> and why a live view is *allowed* to drop data while a durable sink never is.
> No prior knowledge of SSE or fan-out patterns assumed.
>
> This prepares you for **V4** in [SPEC.md](../SPEC.md) — the
> [`stream()`](../src/sse.rs) response you'll build on top of the already-wired
> [`LiveFeed`](../src/sse.rs) hub, serving `GET /stream` in
> [routes.rs](../src/routes.rs). Card 4 in [CONCEPTS.md](../CONCEPTS.md) is the
> checklist this doc unlocks.

---

## 0. The one sentence to hold onto

**There are two backpressure regimes, and every stream must be classified into
exactly one: a durable path must never drop, so it slows the producer; a live
path must never slow the producer, so it drops.** V3 built the first; V4
builds the second — and the *contrast* is the lesson.

---

## 1. The problem: the suspended Chrome tab that owns your pipeline

Every closed rollup window should appear, live, on every open dashboard. One
producer (the pipeline flushing windows), N consumers (browser tabs). Now one
of those tabs is backgrounded on a laptop with its lid closed. TCP keeps the
connection alive, but nothing drains it. If your fan-out waits for every
subscriber:

| Waiting-for-everyone fan-out | What breaks |
| --- | --- |
| Producer `await`s the slowest send | The flush loop in [pipeline.rs](../src/pipeline.rs) stalls — the *same* loop that feeds the durable sink |
| Windows stop closing | Rollup map grows; ClickHouse writes stall; consumer lag climbs |
| Net effect | **Your storage path now runs at the speed of someone's suspended Chrome tab** |

The mechanism that *saved* you in V3 — block the producer when a consumer is
slow — is exactly what would kill you here. Same mechanism, opposite verdict.
Why?

---

## 2. The two-regimes rule: classify the data, then pick the policy

Ask one question of any stream: **if a consumer misses an item, is state
corrupted — or is a frame just missing until the next update replaces it?**

| | Durable path (V3: rollups → ClickHouse) | Live path (V4: rollups → dashboards) |
| --- | --- | --- |
| A gap means | A hole in history, forever queryable as wrong | A chart misses one frame; the next window redraws it |
| Therefore | **Never drop** → bounded buffer *slows the producer*; backlog parks in the broker | **Never slow the producer** → bounded buffer *sheds the laggard* |
| Who absorbs overload | The broker's disk | The slow consumer's experience |

Both regimes use a *bounded* buffer — unbounded is wrong in both worlds
(that's the V3 time-bomb). What differs is **what happens at the bound**:
block upstream, or drop for that subscriber. The trap Card 4 names is serving
both regimes from one code path "for DRY" — the policies are opposites, and
sharing a queue between the sink and the fan-out re-couples what V3/V4 exist
to decouple. In the scaffold they're already separate: the sink's buffer in
[sink.rs](../src/sink.rs) versus the broadcast hub in [sse.rs](../src/sse.rs).

---

## 3. The transport: what SSE actually is

Server-Sent Events is almost embarrassingly simple, and that's its virtue: a
normal HTTP response with `Content-Type: text/event-stream` that just…
never ends. The body is a stream of text frames; a blank line terminates each
event. What one closed window looks like on the wire:

```
retry: 3000

id: 1719600060
data: {"series_id":1846...,"measurement":"cpu","window_start":"2024-06-28T18:40:00Z","window_secs":60,"count":58,"sum":39.2,"min":0.41,"max":0.97,"p50":0.66,"p99":0.95}

id: 1719600120
data: {"series_id":1846..., ...}

```

The four field types, each doing one job:

| Field | Job |
| --- | --- |
| `data:` | The payload (here: one [`RollupRow`](../src/model.rs) as JSON) |
| `id:` | Names this event; the browser remembers the last one it saw |
| `retry:` | Tells the browser how long (ms) to wait before auto-reconnecting |
| `event:` | Optional type label (e.g. an `event: lag` notice — see §5) |

The reconnect loop is built into the browser's `EventSource`: connection
drops → wait `retry:` ms → reconnect **with header `Last-Event-ID: <last id
seen>`** → your handler can resume from there. You get reconnection and
resume *protocol* for free; [routes.rs](../src/routes.rs) already extracts
the header and hands it to [`sse::stream()`](../src/sse.rs).

**Why SSE and not WebSocket?** Classify the traffic: dashboards are
one-directional (server→client), and SSE rides plain HTTP — every proxy and
load balancer already understands it, auth is just the normal request auth,
and reconnect is free. WebSocket buys bidirectionality (chat, games,
collaborative editing) at the cost of a protocol upgrade and hand-rolled
reconnection. Rule of thumb: *push-only → SSE; conversation → WebSocket.*
(It's why LLM token streaming is SSE everywhere.)

---

## 4. The hub: broadcast fan-out with lagged-receiver shedding

Inside the process, the fan-out hub is already wired:
[`LiveFeed`](../src/sse.rs) wraps a `tokio::sync::broadcast` channel
(capacity `SSE_CAPACITY=1024`, from [.env.example](../.env.example)). Its
semantics are the two-regimes policy *encoded in a type*:

- Every subscriber gets its own cursor into a shared ring of the last
  `capacity` values.
- **`send` never waits for receivers.** When the ring is full, the oldest
  value is overwritten — the producer is structurally incapable of being
  blocked by a slow subscriber.
- A subscriber that falls more than `capacity` behind gets
  `Err(Lagged(n))` on its next receive — "you missed `n` items" — and then
  continues from the oldest value still in the ring. It's shed, notified,
  and resumed; never waited for.

Note also [`LiveFeed::publish()`](../src/sse.rs) ignoring the "no receivers"
error — an idle dashboard fleet must not affect the pipeline either. Zero
subscribers and a thousand subscribers cost the producer the same: one
`send`.

Your V4 work in [`stream()`](../src/sse.rs) is the boundary where hub meets
HTTP: turn a receiver into a stream of SSE events, and *handle the `Lagged`
case as policy, not as an error* — skip forward, count it (the SPEC's
observability list wants an `sse_dropped_for_lag` counter), optionally tell
the client (`event: lag`), and keep serving. The slow tab gets a gappy chart;
the pipeline never notices.

Worth having an opinion on (Card 4 depth probe): for a *chart*, conflation —
keep only the latest value per series — is strictly better than dropping the
oldest N events blindly, because a chart only renders current state; each
frame supersedes the last. For an *event log* it would be exactly wrong,
because every entry matters. Same drop budget, different data semantics.
`broadcast`'s ring gives you drop-oldest for free; whether to layer
keep-latest-per-series on top is a design choice the SPEC leaves to you.

---

## 5. Reconnect resume, honestly: what can you actually replay?

A viewer's laptop sleeps 30 s, wakes, reconnects with `Last-Event-ID`. What
can you give them? Only what you still have. The broadcast ring holds the last
`capacity` rollups; anything older is gone from the live path *by design* —
this is a lossy stream, and that's fine, because the durable history exists in
ClickHouse.

That's also the answer to the cold-start problem, and why the API is split in
two in [routes.rs](../src/routes.rs):

```
dashboard opens ──▶ GET /query?series=…&from=…&to=…   (historical paint, from ClickHouse — V3's read path)
              └──▶ GET /stream                        (live tail from here on, SSE)
reconnect gap  ──▶ small: resume via Last-Event-ID within your buffer
              └──▶ large (id older than the buffer): re-paint via /query, then re-tail
```

The live feed's job is *recency*, not *completeness*. Completeness is the
durable path's job. Keeping those responsibilities separate is the whole
architecture of V4.

---

## 6. The design space V4 leaves to you

1. **Event id scheme** — what makes a good `id:` for resume (and what
   `Last-Event-ID` can honestly buy given a bounded ring)?
2. **Shed policy** — plain skip-ahead on `Lagged`, a lag notice event, or
   keep-latest-per-series conflation?
3. **Keep-alive** — idle proxies kill silent connections;
   `Sse::keep_alive` exists for this (the [`stream()` notes](../src/sse.rs)
   point at it) — pick an interval.
4. **The resume fallback** — what the client should do when its
   `Last-Event-ID` predates your buffer.

`/hint` for nudges, `/quest` to build it against acceptance tests — including
the SPEC bench's finale: many subscribers served while one deliberately
stalled client is shed without affecting the rest.

---

## 7. Mental-model summary

| Concept | One-liner |
| --- | --- |
| Two regimes | Durable: never drop → slow the producer. Live: never slow the producer → drop. Classify every stream |
| The classification test | Does a gap corrupt state, or just miss a frame the next update replaces? |
| SSE | `text/event-stream`: `data:`/`id:`/`retry:`/`event:` frames, blank-line terminated; browser reconnects + sends `Last-Event-ID` for free |
| SSE vs WebSocket | Push-only over plain HTTP → SSE; bidirectional conversation → WebSocket |
| broadcast hub | Ring buffer per-capacity; `send` never blocks; laggard gets `Lagged(n)` + skip-ahead — the shed policy in a type |
| Conflation | Keep-latest-per-series beats drop-oldest for charts (state), and is wrong for logs (events) |
| Cold start | `/query` paints history from ClickHouse; `/stream` keeps it live; big gaps re-paint rather than replay |

## 8. Where you'll build this

- [`sse::stream()`](../src/sse.rs) — the one `todo!()`: receiver → SSE
  response, with `Lagged` handled as shed-and-count, keep-alive, `retry:`,
  and `Last-Event-ID` honoured where feasible.
- The hub ([`LiveFeed`](../src/sse.rs)) and the publish call in
  [`flush_windows()`](../src/pipeline.rs) are already wired — study why
  `publish` can never block before you write the handler.

You own it (Card 4 of [CONCEPTS.md](../CONCEPTS.md)) when you can explain:
the two-regimes rule and how to classify a stream; SSE vs WebSocket for this
job; the wire mechanics including resume; what happens to a lagged
subscriber and why that's correct product behavior; and why `/query` + SSE
split the cold-start problem.
