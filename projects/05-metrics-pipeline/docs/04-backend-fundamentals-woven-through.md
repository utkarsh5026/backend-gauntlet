# Backend Fundamentals Woven Through This Project

> The horizontal-checklist ideas that don't belong to any single vertical but
> show up in all of them: what `202 Accepted` really promises, why an open
> `/ingest` is a weapon, how a metrics pipeline watches *itself*, and what a
> clean shutdown owes the data. No prior knowledge assumed.
>
> Grounded in the [SPEC's horizontal checklist](../SPEC.md) and the ⚡
> rapid-fire round of [CONCEPTS.md](../CONCEPTS.md), anchored to
> [routes.rs](../src/routes.rs), [pipeline.rs](../src/pipeline.rs), and
> [error.rs](../src/error.rs).

---

## 1. `202 Accepted`: a status code that tells the truth

Look at what [`ingest`](../src/routes.rs) returns on success: not `200 OK`,
not `201 Created` — `202 Accepted`. This is precise, not pedantic. Walk the
timeline of one point:

```
client POSTs line ─▶ parse OK ─▶ published to JetStream ─▶ [202 returned HERE]
                                        │
                                        └─… seconds later: consumed, rolled up,
                                            batched, written to ClickHouse
```

At response time the point is **durably enqueued but not yet stored or
queryable**. Each code makes a different promise:

| Code | Its promise | Honest here? |
| --- | --- | --- |
| `200 OK` / `201 Created` | Processing finished / the resource exists | No — a `/query` right now won't see the point |
| `202 Accepted` | "Accepted for processing; not done yet" | Exactly the truth |

The deeper design point: making the client wait for ClickHouse would couple
ingest latency to store latency — the exact coupling the broker exists to
break. The write path answers as soon as durability is achieved, and *where*
durability is achieved (the broker, not the store) is what `202` encodes.
The same honesty rule drives the rest of the status map through
[`AppError`](../src/error.rs): `400` for a malformed line or bad query
params, `404`/`204` for an empty range — each code a claim you can defend.

---

## 2. Security: `/ingest` is an attack surface with a metrics-shaped twist

The horizontal checklist's security items look generic (authenticate, validate
input) until you notice the abuse vector unique to metrics. What can an
unauthenticated caller do to an open `/ingest`?

| Attack | Mechanism | Defense |
| --- | --- | --- |
| Forge metrics | Fake points → false dashboards, false alerts (or silenced real ones) | Authenticate ingest (API key/token) — and the query/stream side too, since dashboards can leak topology |
| Oversized payloads | Huge bodies/lines chew parse CPU and RAM | Cap body size, line length, points per request ([the `ingest` TODO](../src/routes.rs)) |
| **Cardinality bomb** | One metric with a `rand_id=<uuid>` tag → every point a *new series* → rollup map and store grow without bound | Cap tag count/length/charset, and a **per-tenant cardinality ceiling** — the cap that's *about* the data model, see [doc 00](00-the-time-series-data-model-and-cardinality.md) §4 |
| Absurd timestamps | A point dated 1970 lands in partition `19700101` (the table [partitions by day](../migrations/0001_init.sql)) — a partition nothing will ever query or retire sanely; far-future ones dodge TTLs | Reject timestamps outside a sane window at parse time ([the parse TODO](../src/parse.rs)) |
| PII in tags | Tags can carry emails, tokens, URLs | Never log raw payloads |

The cardinality bomb is the one to internalize: it's a *denial of service via
data model*, no flood required — a trickle of well-formed, novel-tagged points
does it. That's why the ceiling is per-tenant (one bad client shouldn't spend
the whole system's series budget) and why it needs auth to exist at all: no
identity, no tenant to meter.

---

## 3. Observability: the pipeline must eat its own dog food

A metrics pipeline that can't observe itself is the last thing to alert on its
own death. The SPEC's list isn't a grab-bag — each gauge is a **leading
indicator of a specific failure**, most of them already exposed as methods in
the scaffold waiting to be exported:

| Signal | Source in scaffold | A rising line predicts |
| --- | --- | --- |
| Live series cardinality (gauge) | derivable once V1's fingerprint exists | The OOM, days early — the cost function of [doc 00](00-the-time-series-data-model-and-cardinality.md) made visible |
| Open windows (gauge) | [`Rollup::open_windows()`](../src/rollup.rs) | Watermark trouble: late floods or a stuck flush → unbounded map growth |
| Consumer lag (gauge) | JetStream's pending count for the durable consumer | "We're falling behind": consumption slower than ingest — the backlog is parking in the broker (by design) but growing (needs action) |
| Batch fill ratio (gauge) | [`Sink::pending()`](../src/sink.rs) vs `BATCH_MAX_ROWS` | Always ~0: time-trigger-only flushing (idle, or batch too big). Always full: at the throughput edge |
| SSE clients / dropped-for-lag | [`LiveFeed::subscribers()`](../src/sse.rs) + your V4 counter | Shedding is *working* — and which dashboards are chronically slow |
| Points rejected, by reason (counter) | your V1 parse errors | A broken client, before its data loss is noticed by a human |
| End-to-end lag p50/p99 (histogram) | point timestamp → visible in ClickHouse | The freshness promise your dashboards silently make |

Two habits worth stealing: gauges for *state sizes* (every bounded thing gets
a "how full" signal — if it's worth bounding, it's worth watching), and
counters segmented *by reason* (a rejected-total tells you something's wrong;
`{reason="bad_timestamp"}` tells you what). Note the pleasant recursion: these
metrics are themselves measurements with tags — the system's own data model,
pointed at itself. (Prometheus wiring: project 04 did this; same recipe.)

---

## 4. Graceful shutdown: what a clean exit owes the data

Doc [02](02-the-batched-at-least-once-sink.md) §6 made the distinction — a
*crash* mid-batch is covered by redelivery, but a *clean shutdown* that drops
data it could have flushed is a bug. The SPEC turns that into an ordering,
and the scaffold's shutdown arm in [pipeline.rs](../src/pipeline.rs) already
sketches it:

```
1. stop accepting ingest            (the HTTP server drains first)
2. drain_all() partial windows      (V2 — the TODO on the shutdown arm)
3. final sink.flush()               (V3 — the last batch goes out)
4. ack what was written
5. drain SSE clients, exit
```

The order is the point: each step hands its data to the next stage before
that stage closes. Reverse any pair and you leak — flush before drain and
the drained windows miss the batch; exit before ack and the broker
redeliver-duplicates work you *did* finish (harmless here thanks to V3's
idempotency, but wasted). Shutdown is where all three verticals' contracts
meet, which is why it's a horizontal item and not a vertical.

---

## 5. Mental-model summary

| Fundamental | One-liner |
| --- | --- |
| `202 Accepted` | "Durably enqueued, not yet stored" — the status code encodes *where* durability was achieved |
| Auth on ingest | No identity → forged data, and no tenant to hang a cardinality budget on |
| Cap what callers control | Line length, tags, points/request — and the per-tenant series ceiling, the metrics-specific DoS defense |
| Timestamp sanity | Absurd times poison partitions and dodge TTLs; reject at the door |
| Dog-fooding | Gauge every bounded thing, segment counters by reason; each signal predicts a named failure |
| Graceful shutdown | Drain → flush → ack → exit, in that order; redelivery is the net for crashes, not for laziness |

These aren't extra credit — the horizontal checklist in
[SPEC.md](../SPEC.md) gates the Definition of done alongside the verticals,
and the `bench/` load test will need the observability half to even produce
its numbers.
