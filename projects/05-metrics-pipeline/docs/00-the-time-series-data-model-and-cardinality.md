# The Time-Series Data Model & Cardinality — From First Principles

> What a "metric" actually *is*, why two tag orderings must be the same series,
> and why one careless tag can OOM the whole system. No prior knowledge of
> metrics systems assumed.
>
> This prepares you for **V1** in [SPEC.md](../SPEC.md) — the parser and
> fingerprint you'll build in [parse.rs](../src/parse.rs), on top of the types
> in [model.rs](../src/model.rs). Card 1 in [CONCEPTS.md](../CONCEPTS.md) is the
> checklist this doc exists to unlock.

---

## 0. The one sentence to hold onto

**The unit of cost in a metrics system is not the point — it's the *series*:
a measurement plus its exact, canonically-ordered set of tags.** Every distinct
tag combination is a new series the pipeline must track in memory, and the
number of series multiplies across tags. Get series *identity* wrong and every
graph is silently corrupted; let series *count* grow unbounded and the system
melts.

---

## 1. The problem: "cpu is at 91%" needs a shape

Suppose you just store what the client sent, as-is:

```
"cpu on host-a in us-east: usage 0.91 at 18:40:00"
```

A row of free text, or even a `(name, value, timestamp)` triple. Now try to
answer real questions with it:

| You want to ask | Why the naive shape fails |
| --- | --- |
| "average cpu **per region**" | Region is buried inside the name string. You'd parse strings at query time, per row, over billions of rows. |
| "cpu for **host=a only**" | Same — no first-class dimension to filter on. |
| "is `cpu{host=a}` the *same* line on the graph as yesterday?" | Nothing defines what "the same metric" *is*. If today's agent sends `region` before `host`, is that a new metric? |
| "how much memory will tracking all this take?" | Unanswerable — there's no unit to count. |

So the model needs **dimensions you can filter and group by** (host, region),
separated from **what is measured** (cpu) and **the observation itself** (a
value at a time). That's the whole data model, and it's already in
[model.rs](../src/model.rs):

```rust
pub struct Series {
    pub measurement: String,              // WHAT is measured: "cpu"
    pub tags: Vec<(String, String)>,      // DIMENSIONS: host=a, region=us — sorted by key
}

pub struct MetricPoint {
    pub series: Series,
    pub value: f64,                       // the observation
    pub timestamp: DateTime<Utc>,         // when it happened
}
```

Four parts: **measurement, tags, value, timestamp**. Prometheus, InfluxDB,
Datadog — all of them are this model with different syntax.

---

## 2. The wire format: one line, several points

Your parser's input ([parse.rs](../src/parse.rs)) is a *line protocol*, the
InfluxDB shape:

```
measurement,tag1=v1,tag2=v2  field1=val1,field2=val2  timestamp
cpu,host=a,region=us         usage=0.91,sys=0.12      1719600000
└┬┘ └──────┬───────┘         └────────┬─────────┘     └───┬────┘
 what  dimensions              the observations       unix seconds
                                                      (2024-06-28 18:40:00 UTC)
```

Three space-separated sections. Note the asymmetry: tags ride with the
measurement (comma-joined, no space), fields are their own section, timestamp
is optional (default: ingest time — a V1 decision the doc-comment in
[model.rs](../src/model.rs) calls out).

**One line with N fields expands to N `MetricPoint`s** — `usage` and `sys` are
different things being observed, so each is its own point (and its own series):

| # | measurement | tags | value | timestamp |
| --- | --- | --- | --- | --- |
| 1 | `cpu` (+field `usage`) | `host=a, region=us` | 0.91 | 2024-06-28 18:40:00 UTC |
| 2 | `cpu` (+field `sys`) | `host=a, region=us` | 0.12 | 2024-06-28 18:40:00 UTC |

*How* the field name folds into the series — into the measurement (`cpu_usage`)
or as a reserved tag (`__field__=usage`) — is **your** modelling choice, flagged
in the [`MetricPoint` docs](../src/model.rs). Both work; what matters is that
the choice is *canonical* (one rule, applied everywhere), because it feeds the
fingerprint below.

---

## 3. Series identity: the fingerprint, and why sorting is load-bearing

A **series** is "measurement + exact tag set". `cpu{host=a}` and `cpu{host=b}`
are different series — different lines on the graph, different rollup buckets,
different rows in ClickHouse. Every later stage keys on this identity, so it
needs a stable, compact form: a `u64` fingerprint
([`SeriesId`](../src/model.rs)), which you'll compute in
[`fingerprint()`](../src/parse.rs) — currently a `todo!()`.

Here's the trap. Two agents send the *same* series with tags in different
order:

```
cpu,host=a,region=us usage=0.91     ← agent 1
cpu,region=us,host=a usage=0.88     ← agent 2 (same series!)
```

Hash the tag string *as it arrived* and you get two different ids. Real
FNV-1a-64 values over the raw concatenations (computed, not guessed):

| Input hashed | FNV-1a 64 |
| --- | --- |
| `cpu\|host=a\|region=us` | `1981cd1cd036e468` |
| `cpu\|region=us\|host=a` | `fd0ae0e94c119e6a` |

Different hashes ⇒ **one real series splits into two**. Every graph of it shows
half the truth; the rollup engine keeps two windows where there should be one;
ClickHouse stores two rows. Nothing errors. That's corruption mode #1.

Corruption mode #2 is the mirror image: a sloppy canonicalization that maps two
*different* tag sets to one string **merges two series**, silently blending
unrelated data into one line.

The fix is in the `Series` invariant already documented in
[model.rs](../src/model.rs): **tags are sorted by key before hashing.** Then
both agents' lines canonicalize to `cpu|host=a|region=us` and collide on
purpose. Sorting is not a tidiness nicety — it's what makes identity exist.

One more subtlety the [`fingerprint()` notes](../src/parse.rs) name: you need a
**separator** between hashed parts. Without one, different splits concatenate
to the same bytes and *collide by construction* — again real values:

| Parts | Bytes hashed | FNV-1a 64 |
| --- | --- | --- |
| `ab` + `c` (no separator) | `abc` | `e71fa2190541574b` |
| `a` + `bc` (no separator) | `abc` | `e71fa2190541574b` ← same! |
| `ab` \| `c` | `ab\|c` | `fc438883ee2c38b3` |
| `a` \| `bc` | `a\|bc` | `0e4e0283f89820af` |

And the hasher itself must be **stable across processes** — Rust's
`DefaultHasher` is randomly seeded per process, so ids computed today wouldn't
match ids persisted in ClickHouse yesterday. Which hasher, and exactly what
byte layout you feed it, is the part *you* decide in V1.

---

## 4. Cardinality: the cost function of the entire system

**Cardinality = the number of distinct series.** It is the true cost metric of
a metrics pipeline, and it *multiplies*: every tag contributes a factor of its
distinct-value count.

A perfectly reasonable HTTP-latency metric:

```
tags: host (20) × region (3) × endpoint (40) × status_class (5)
    = 20 × 3 × 40 × 5 = 12,000 series
```

12,000 series is fine — one `Aggregate` each in the rollup map, a few MB.

Now one engineer adds `user_id` as a tag, with 50,000 active users:

```
12,000 × 50,000 = 600,000,000 series
```

A 50,000× explosion from one tag. Every one of those series wants an entry in
the in-memory rollup map ([rollup.rs](../src/rollup.rs) — its doc-comment calls
that map "the OOM canary"), an index entry in the store, a row per window in
ClickHouse. This is the classic way real metrics systems die, and it's so real
that Datadog *bills* per distinct series.

The rule of thumb for what may be a tag:

| Belongs in a **tag** | Belongs in the **value**, or in logs/traces instead |
| --- | --- |
| Bounded, enumerable, groupable: `region`, `status_class`, `endpoint` (templated: `/users/:id`, never the raw URL) | Unbounded identifiers: `user_id`, `request_id`, raw URLs, container ids, email addresses |
| "I'd put it in a `GROUP BY`" | "I'd search for one specific one" — that's a log query, not a metric |

The classic cardinality bombs — user id, request id, raw URL, container id —
are all "unbounded identifier smuggled into a dimension".

V1 doesn't force you to *enforce* a ceiling yet, but the SPEC asks the model to
make one **expressible**: to enforce "max N series per tenant" you must be able
to *count* distinct series, which means the parse/fingerprint layer is where a
new-vs-seen series is first knowable (the
[migration file](../migrations/0001_init.sql) sketches a `series` dimension
table for the same reason). Where exactly the check sits — parser, publisher,
or consumer — is a design decision the SPEC's security checklist will make you
defend.

---

## 5. Malformed lines: reject *and count*

The parser is the front door, and [parse.rs](../src/parse.rs) is explicit: a
malformed line is a hard error, never silently skipped, never allowed to
poison the batch. But rejection alone isn't enough — the SPEC says reject **and
count**. Why the counter matters: a client that ships a broken agent version
produces a *stream* of malformed lines. Without a `points_rejected{reason=…}`
counter, that looks like traffic quietly disappearing; with it, a dashboard
line jumps and you find the broken client in minutes. A parse-error metric is
how a pipeline notices a broken producer before the on-call does.

Validation is also the security surface: cap line length, tag count, key/value
charset and length (see the horizontal checklist in [SPEC.md](../SPEC.md)) —
because an attacker who controls tag values controls your cardinality, and §4
just showed what cardinality does.

---

## 6. The design space V1 leaves to you

The concepts above are settled; these decisions are yours, and they're the
interesting part:

1. **The field-name fold** — measurement suffix or reserved tag? (Affects what
   "measurement" means downstream and how `/query` filters.)
2. **The exact canonical byte layout** fed to the hash — separators, and how
   you avoid ambiguity between measurement/keys/values.
3. **Which stable hasher** — and what its collision odds mean at your target
   cardinality.
4. **Timestamp policy** — what counts as "absurd past/future" and gets a `400`.
5. **Where the cardinality ceiling will eventually sit** — and what the model
   must expose to make it enforceable.

When you're ready to build, that's `/quest` (acceptance tests first) or
`/hint` if you're stuck on a specific decision. This doc stops at the door on
purpose.

---

## 7. Mental-model summary

| Concept | One-liner |
| --- | --- |
| Measurement | *What* is measured (`cpu`) — the graph's subject |
| Tag | A bounded dimension you filter/group by — each distinct combo is a new series |
| Value / timestamp | The observation — unbounded data lives here, never in tags |
| Series | measurement + exact tag set: the identity everything downstream keys on |
| Fingerprint | Stable `u64` over measurement + tags **sorted by key**, with separators, from a cross-process-stable hasher |
| Split / merge corruption | Unsorted → one series becomes many; sloppy canon → many become one. Both silent |
| Cardinality | Count of distinct series = ∏ per-tag distinct values. The system's memory & billing cost |
| Reject-and-count | A malformed line is a 400 *and* a counter bump — the broken-client alarm |

## 8. Where you'll build this

- [`fingerprint()`](../src/parse.rs) — the canonical hash (`todo!()`).
- [`parse()`](../src/parse.rs) — line protocol → `Vec<MetricPoint>`, with
  validation caps (`todo!()`).
- The tests sketched at the bottom of [parse.rs](../src/parse.rs): tag order
  must **not** change the id, tag values **must**, malformed lines error
  without panicking.

You own it (Card 1 of [CONCEPTS.md](../CONCEPTS.md)) when you can explain, at a
whiteboard: measurement/tag/value/timestamp and the tag rule of thumb; both
silent corruption modes of a non-canonical fingerprint; cardinality as
multiplication with real numbers; the classic bombs and where a ceiling sits;
and why rejects are counted.
