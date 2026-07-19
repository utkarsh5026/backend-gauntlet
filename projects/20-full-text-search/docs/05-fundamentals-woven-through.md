# The Fundamentals Woven Through This Project — From First Principles

> One doc for the horizontal checklist: the query cache and its invalidation,
> why a Zipfian workload is what makes caching pay, input caps as cost control,
> the deliberate contention model, graceful shutdown, and the six metrics the
> SPEC grades. None of these is a vertical, but every one is real backend
> engineering with real code to write. No prior knowledge assumed. Anchored to
> [cache.rs](../src/cache.rs), [shard.rs](../src/shard.rs),
> [routes.rs](../src/routes.rs), [metrics.rs](../src/metrics.rs), and
> [main.rs](../src/main.rs); graded by the horizontal checklist in
> [SPEC.md](../SPEC.md).

---

## 0. The one sentence to hold onto

**The verticals make one search correct and fast; the horizontals make a
million searches from strangers survivable — memoize the repeats, bound the
inputs, know your locks, exit cleanly, and measure everything you'd otherwise
have to guess.**

---

## 1. The query cache: memoizing the whole pipeline

### The problem

Follow one search through the finished engine: analyze the query (V1), fan out
to every shard (V5), walk postings in every segment (V2), BM25-score every
match (V3), heap-merge top-ks. Now the same user — or ten thousand different
users — sends the *same query* two seconds later, against an index that hasn't
changed. Every recomputed step produces byte-identical results. That's not
work; that's a lookup you forgot to do.

### Why real search traffic makes this pay: Zipf

Cache effectiveness is a property of the *workload*, not the cache. Search
traffic is famously **Zipfian**: frequency ∝ 1/rank — a tiny head of white-hot
queries and an endless tail of one-offs:

```
 query freq
    █
    █ ▄                       the head: a handful of queries = most of the traffic
    █ █ ▂                      → each cache entry serves thousands of hits
    █ █ █ ▁ ▁ . . . . . . .   the tail: seen once, never again
    └────────────────────────  → caching them is pointless; they keep the
      1 2 3 4 5 …    rank        engine honest (every miss runs the full path)
```

That's why the boss fight ("The Long Tail") generates Zipfian queries and
demands **hit ratio ≥ 80%** with the cache-on run beating cache-off by **≥ 3×**
on the hot set: on this distribution a small cache absorbs the head, and the
uncacheable tail is exactly what stresses your verticals. A cache that only
helps on uniform traffic helps nowhere real.

### What's keyed, and where the cache sits

Look at what [`ShardedIndex::search`](../src/shard.rs) already does: it
consults the cache **before** analysis, keyed on `(k, raw query)`
([`cache_key`](../src/shard.rs)), and stores the final *merged* hits after the
gather. So this is a **coordinator-level result cache** — a hit does zero
analysis, zero fan-out, zero scoring (the horizontal's literal criterion: "a
cache hit does zero scoring"). The scaffold note points at a refinement worth
weighing: keying on raw text means `"Rust!"` and `"rust"` cache separately even
though they analyze identically — keying on analyzed terms would merge them, at
the cost of running V1 before the lookup. That's your call to make and document.

Your `todo!()`s are the store itself — [`QueryCache`](../src/cache.rs):
`get`, `put` (bounded, with an eviction rule — the scaffold sketches LRU), and
`invalidate_all`. Note `cap == 0` disables the whole thing
([`enabled`](../src/cache.rs)), so nothing blocks the verticals.

### Invalidation: the half that decides correctness

A cached result is a **claim about the index at a moment in time**. The moment
the searchable set changes, the claim may be false — and a stale hit is worse
than a miss, because a miss costs milliseconds while a stale hit *returns a
document the user deleted* (or hides one they just added past its refresh).
What changes the searchable set? Exactly three events, all of which you built:

| Event | Why cached results go stale |
| --- | --- |
| **refresh** (V2) | New docs became searchable; a cached top-k may now be missing a better hit |
| **merge** (V4) | Tombstoned docs physically vanish; also transparent to results — but paired with deletes it matters |
| **delete** (V4) | A cached result may contain the tombstoned doc — the worst kind of stale |

The scaffold already calls [`invalidate_all`](../src/cache.rs) from
`refresh_all` and `force_merge` (see [shard.rs](../src/shard.rs)) — the simple,
correct policy: nuke everything on any change. Crude? Deliberately: search
results are only as fresh as the last refresh anyway, so refresh is the natural
epoch boundary. Finer policies (per-segment generation stamps, per-term
invalidation) buy hit-ratio across refreshes at real complexity — the SPEC
leaves them as stretch, alongside **single-flight** (when 100 concurrent
requests miss on the same hot query, let one do the fan-out and 99 wait for its
result — otherwise a popular query's expiry triggers a herd, project 01's
thundering-herd lesson replayed).

One correctness footnote for the concurrent world: whatever store you build
lives behind `&self` and is hit by many tasks at once — the scaffold hands you
a `Mutex<CacheInner>` and the discipline is keeping its critical sections tiny
(never hold it across the fan-out).

---

## 2. Input caps: correctness for someone else's worst input

Both caps are enforced at the coordinator, before any real work — and both
already have their check written; understanding *why* is the checklist item:

- **`MAX_DOC_BYTES`** ([`add_document`](../src/shard.rs)): indexing cost is
  linear in document size — analysis walks every byte, every term becomes
  postings. An unbounded document is an unbounded write amplified by every
  future merge it survives (V4). One 2 GiB "document" shouldn't be able to eat
  your buffer, your refresh, and your merge I/O.
- **`MAX_QUERY_TERMS`** ([`search`](../src/shard.rs), *after* analysis —
  counting analyzed terms, not raw words, so the cap can't be dodged with
  punctuation): each query term is a dictionary lookup + postings walk *per
  segment per shard* (V2×V5). A 10,000-term query is a scan of everything
  wearing a query costume. Bounding terms bounds the fan-out — this is
  query-cost control, the same instinct as bounding `size` so no request can
  ask for the whole corpus.

The pattern to internalize: **every request-shaped number needs a ceiling**,
and the ceiling belongs at the edge, before the expensive machinery. The
remaining TODO in this family is auth on write/admin routes (see the
`TODO(security)` notes in [routes.rs](../src/routes.rs)): an open `/documents`
endpoint is an open disk for the internet, search stays public, and keys never
appear in logs or errors.

---

## 3. The contention model: state your locks out loud

The checklist asks for something unusual: not code, but a *deliberate* answer
to "who blocks whom?" This project's answer, assembled from the verticals:

```
   READS (search)                         WRITES (index)
   ────────────────                       ─────────────────
   segments: immutable files              in-memory buffer: one writer
   read via Arc<SegmentReader>            at a time per shard
   → ANY number of concurrent             → serialized, deliberately
     searches, ZERO locks                   (contention confined here)
   tombstones: LiveDocs overlay           refresh: swaps buffer → new
   (a lookup per posting)                 segment, atomically
```

Reads scale with cores because immutability (V2) removed the need to
coordinate; writes serialize per shard because a single buffer is simple and
the write path isn't the bottleneck search traffic cares about; sharding (V5)
multiplies both. The reason this is a checklist item: most systems have a
contention model *by accident* — a lock added here, an `RwLock` there — and
discover it under load. Yours is on purpose, and you can say where every lock
is and why. (You'll notice the pattern's name elsewhere: MVCC, copy-on-write,
epoch-based reclamation — "readers never wait" is a family of designs, and you
built one.)

---

## 4. Graceful shutdown: the buffer is a promise you must keep or retract

Ctrl-C arrives. Two things are in flight: HTTP requests mid-handler, and — the
search-specific one — **buffered documents that were accepted with a 201 but
never refreshed into a segment** (V2's NRT contract). Kill the process and they
evaporate: the client was told "created" and the data is gone.

The scaffold wires the mechanics ([main.rs](../src/main.rs) has the signal
handling and a `watch` channel broadcasting shutdown to background tasks); the
checklist grades the *policy*, and offers an honest pair:

1. **Drain + final refresh** — stop accepting, finish in-flight requests, flush
   every shard's buffer one last time. A clean shutdown loses nothing.
2. **Or state the contract explicitly** — "un-refreshed documents are not
   durable; a crash or shutdown may drop them." Elasticsearch's real answer is
   a translog (write-ahead journal) making buffered docs crash-safe — out of
   scope here, which is exactly why the SPEC accepts a *stated* weaker contract.

What's not acceptable is the implicit maybe — durability semantics decided by
whoever kills the process. Either behavior, written down, passes; silence
fails. (Note the asymmetry with crashes: a final refresh saves a *clean*
shutdown only. Your design doc should say which failures the contract covers.)

---

## 5. Observability: the six numbers that answer "is it healthy?"

The metric names are single-sourced in [metrics.rs](../src/metrics.rs); the
`/metrics` endpoint already renders ([`metrics_router`](../src/routes.rs)).
Your work is wiring the **call sites** — the `TODO(observability)` markers in
[shard.rs](../src/shard.rs) and [index.rs](../src/index.rs). Each series earns
its place by answering a question you'll actually ask during the boss fight:

| Metric | The question it answers | The moment you'll need it |
| --- | --- | --- |
| `search_searches_total` | How much traffic? | The 2,000/sec target is this counter's rate |
| `search_duration_seconds` (histogram) | What does the *slow* experience look like? | p99 ≤ 50 ms is a quantile of this — averages hide the tail (V5's whole lesson) |
| `search_query_cache_lookups_total{outcome}` | Is the cache earning its memory? | hit ratio ≥ 80% = `hit / (hit + miss)` |
| `search_segments{shard}` (gauge) | Am I merging enough? | **The** health gauge: climbs on refresh, drops on merge; drifting up = V4's policy is losing |
| `search_documents_indexed_total` | Is the background writer alive? | The boss fight indexes continuously — a flat line here invalidates the run |
| `search_merges_total` | Are merges actually happening? | Cross-check the segment gauge: bounded segments + zero merges = you're not indexing |

Two habits the checklist encodes beyond the numbers: a `tracing` span per
request with a request id (already layered via `common-telemetry` in the
router — so a slow search is *findable*, not just countable), and structured
log lines on the three events that matter (flush, merge, delete) — the events
whose timing explains every mystery the metrics surface. The meta-lesson: you
instrument *before* the load test, because during one is too late — every boss
fight criterion above is read off these series, not off a stopwatch.

---

## 6. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Result cache | Memoize the *whole* pipeline at the coordinator: `(k, query) → merged hits`; a hit does zero scoring. |
| Zipf | A tiny hot head makes caching pay; the one-off tail keeps the engine honest. Hit ratio is a workload property. |
| Invalidation | A cached result is a claim about an index-moment; refresh/merge/delete end the moment — nuke on epoch, and a stale hit is worse than a miss. |
| Single-flight (stretch) | One miss does the work; the other 99 concurrent misses wait for it. |
| Input caps | Every request-shaped number gets a ceiling at the edge: doc bytes bound write cost, query terms bound fan-out. |
| Contention model | Immutable segments → lock-free reads; one buffer writer per shard → confined write contention. On purpose, stated. |
| Graceful shutdown | Drain + final refresh, or an explicit "un-refreshed ≠ durable" contract. Never the implicit maybe. |
| The six metrics | Traffic, latency histogram (p99), cache ratio, segment gauge, docs indexed, merges — each maps to a boss-fight criterion. |

## 7. Where you'll build this

- **Modules:** [src/cache.rs](../src/cache.rs) (`get`/`put`/`invalidate_all`
  `todo!()`s), the `TODO(security)` auth checks in
  [src/routes.rs](../src/routes.rs), and the `TODO(observability)` call sites
  in [src/shard.rs](../src/shard.rs) + [src/index.rs](../src/index.rs).
- **Unlocks these horizontal boxes** ([SPEC.md](../SPEC.md)): the three Caching
  items, both Security items, all three Observability items, the contention
  model, and the graceful-shutdown Protocols item.
- **Feeds the boss fight directly:** hit ratio ≥ 80%, cache-on ≥ 3× cache-off,
  p99 ≤ 50 ms, segments ≤ 20 — all read from the metrics you wire here, with
  the cache policy recorded in `docs/20-design.md`.
