# BM25: Ranking, Not Just Matching — From First Principles

> A ground-up guide to relevance scoring: why matching alone is useless, why the
> obvious score (TF-IDF) fails in two ways users feel immediately, and how BM25's
> two corrections fix it. No information-retrieval background assumed. This
> prepares you for **V3** in [SPEC.md](../SPEC.md): the `todo!()`s in
> [`Bm25::score`](../src/bm25.rs) and [`Bm25::search`](../src/bm25.rs) are what
> you're about to build. Anchored to [bm25.rs](../src/bm25.rs) and
> [doc.rs](../src/doc.rs). All numbers below are machine-computed, not eyeballed.

---

## 0. The one sentence to hold onto

**BM25 is TF-IDF with two corrections — repetition stops paying (saturation,
`k1`) and length stops winning (normalization, `b`) — and returning the best `k`
of a million matches is a bounded-heap problem, never a sort.**

---

## 1. The problem: a set is not an answer

V2 gives you matching: walk the postings for each query term and you know *which*
documents contain them. For the query `rust fast` over a real corpus that's
50,000 documents. A user reads ten. Which ten? Matching has no opinion — ranking
is the entire product.

So: assign every matching document a score, return the highest. What should score
high? Two intuitions, each half of TF-IDF:

- **TF (term frequency)** — a document that says `rust` five times is probably
  more about Rust than one that mentions it once. *Frequency within the doc
  signals aboutness.*
- **IDF (inverse document frequency)** — in the query `the rust book`, matching
  `rust` means far more than matching `the`, because `the` is in every document.
  *Rarity across the corpus signals discrimination.*

Naive TF-IDF multiplies them: `score(d) = Σ over query terms t of tf(t,d) × idf(t)`.
Reasonable — and broken in two ways users feel on day one:

| Naive TF-IDF | What breaks | Who exploits it |
| --- | --- | --- |
| TF is **linear** | A doc saying `rust` 100 times scores 100× one saying it once — as if it were 100× more relevant | Keyword stuffers. Every SEO-spam page ever. |
| No notion of **length** | A 10,000-word page accumulates high TFs by sheer surface area and buries the focused 200-word answer | Any long, rambling document — accidentally |
| (and structurally) | Scoring 50k matches then **sorting all of them** to return 10 | Your own query loop, at scale |

BM25 (Okapi BM25, from the 1990s) fixes the first two with one parameter each,
and the third is fixed by *how you collect*, not how you score. It is the literal
default scorer in Lucene, Elasticsearch, OpenSearch, and tantivy, and still the
baseline every neural-retrieval paper has to beat.

---

## 2. The formula, derived part by part

The scaffold's doc comment in [bm25.rs](../src/bm25.rs) states the target:

```text
score(d, q) = Σ over t in q of:
    idf(t) · ( f(t,d) · (k1 + 1) ) / ( f(t,d) + k1 · (1 − b + b · |d| / avgdl) )

idf(t) = ln( 1 + (N − n(t) + 0.5) / (n(t) + 0.5) )
```

Symbols: `f(t,d)` = term frequency in the doc, `|d|` = doc length (in analyzed
tokens — V1's `AnalyzedDoc.length`), `avgdl` = average doc length, `N` = docs in
the collection, `n(t)` = docs containing `t`. Don't memorize it — watch each
piece earn its keep.

### 2a. Saturation: `tf / (tf + k1)` — the 10th occurrence is nearly free

Replace linear TF with a **ratio that approaches a ceiling**. Ignore length for a
moment (assume `|d| = avgdl`, so the normalizer is 1); the TF part becomes
`tf·(k1+1) / (tf + k1)`. At the Lucene-default `k1 = 1.2`:

| tf | contribution | vs linear |
| --- | --- | --- |
| 1 | 1.000 | 1× |
| 2 | 1.375 | 2× would be 2.0 |
| 3 | 1.571 | |
| 10 | 1.964 | 10× would be 10.0 |
| 100 | 2.174 | 100× would be 100.0 |
| ∞ | → k1 + 1 = 2.2 | the ceiling |

```
 contribution
 2.2 ┤ · · · · · · · · · · · · · ·  ← asymptote k1+1
 2.0 ┤              ●────────●
 1.6 ┤       ●──────
 1.4 ┤    ●
 1.0 ┤  ●
     └──┬──┬───┬────┬─────────┬──── tf
        1  2   3    10        100
```

The curve rises fast then flattens: the 2nd occurrence adds 0.375, occurrences
3→10 add ~0.4 *combined*, and 10→100 adds ~0.2. Stuffing `rust` a hundred times
buys almost nothing over saying it ten times. **`k1` sets how fast the ceiling
arrives**: `k1 = 0` collapses the whole fraction to 1 for any tf ≥ 1 — pure
presence/absence, IDF-only scoring; large `k1` approaches linear TF. The default
1.2 (the scaffold's [`Bm25Params::default`](../src/bm25.rs)) is decades of
empirical tuning.

Note what saturation does **not** break: the score is still *monotonic* in tf —
more occurrences never lower it. That's exactly the property test V3 demands
(`prop_score_monotone_in_tf`): saturating, but non-decreasing.

### 2b. Length normalization: `1 − b + b·(|d|/avgdl)` — judged per unit of length

The fix for long-document bias: scale the *denominator's* `k1` by how long this
document is relative to average. The factor at `b = 0.75`, `avgdl = 100`:

| doc length | factor | effect |
| --- | --- | --- |
| 50 (half avg) | 0.625 | denominator shrinks → score **boosted** |
| 100 (average) | 1.000 | neutral |
| 200 (2× avg) | 1.750 | score dampened |
| 400 (4× avg) | 3.250 | strongly dampened |

`b` interpolates between two worlds: `b = 0` makes the factor 1 always
(length-blind — raw saturation), `b = 1` judges tf fully relative to length. When
would you *lower* `b`? A corpus where length carries no signal — tweets, titles,
log lines, all roughly uniform. When lengths vary wildly (web pages), high `b`
protects the focused answer from the rambling one. This is the `k1`/`b` choice
`docs/20-design.md` must record.

### 2c. IDF: the discrimination weight

`idf(t) = ln(1 + (N − n + 0.5)/(n + 0.5))` — big when the term is rare, near
zero when it's everywhere. At `N = 1,000,000` (computed, not guessed):

| n(t) — docs containing t | idf |
| --- | --- |
| 1 | 13.41 |
| 10 | 11.46 |
| 1,000 | 6.91 |
| 500,000 | 0.69 |
| 900,000 | 0.11 |

A term in 90% of documents contributes ~1% of what a 10-doc term does, at equal
tf — which is why `the` scores ~nothing at any frequency even if your analyzer
keeps it. The `+0.5`s smooth the edges and the `1 +` inside the `ln` keeps it
positive even for absurdly common terms (Lucene's exact variant). Where do `N`
and `n(t)` come from in this codebase? `N` and total length ride in each
segment's footer ([`SegmentReader::doc_count`/`total_length`](../src/segment.rs)),
summed into [`CollectionStats`](../src/doc.rs); `n(t)` is the summed
`doc_freq` across segments — the scaffold's `search` TODO spells out that wiring.

Mind the edges the scaffold warns about: an empty collection has
`avg_doc_len = 0` ([`CollectionStats::avg_doc_len`](../src/doc.rs) returns 0.0)
and a never-seen term has `doc_freq = 0` — neither may become a NaN or a
divide-by-zero in your `score`.

---

## 3. The whole thing by hand: a 3-document corpus

The SPEC demands "a hand-computed tiny-corpus ranking the code reproduces" —
here's one worked end to end (compute your *own* for the proof; the point is
doing it once makes the formula yours). Post-analysis corpus, `k1 = 1.2`,
`b = 0.75`:

| doc | terms (after analysis) | length | contains |
| --- | --- | --- | --- |
| d1 | `rust×2, fast, safe` | 4 | the focused doc |
| d2 | `go, fast` | 2 | matches only `fast` |
| d3 | `rust×2` buried in filler | 12 | same tf as d1, 3× longer |

Collection: `N = 3`, `avgdl = (4+2+12)/3 = 6.0`. Both query terms appear in 2 of
3 docs, so `idf(rust) = idf(fast) = ln(1 + (3−2+0.5)/(2+0.5)) = ln(1.6) ≈ 0.4700`.

Query: **`rust fast`**. Per-term pieces (`norm` = `1−b+b·|d|/avgdl`, `sat` =
`tf·2.2/(tf + 1.2·norm)`):

| doc | term | tf | norm | sat | × idf | term score |
| --- | --- | --- | --- | --- | --- | --- |
| d1 (len 4) | rust | 2 | 0.750 | 1.5172 | 0.4700 | 0.7131 |
| d1 | fast | 1 | 0.750 | 1.1579 | 0.4700 | 0.5442 |
| d2 (len 2) | fast | 1 | 0.500 | 1.3750 | 0.4700 | 0.6463 |
| d3 (len 12) | rust | 2 | 1.750 | 1.0732 | 0.4700 | 0.5044 |

Totals: **d1 = 1.2573 > d2 = 0.6463 > d3 = 0.5044.**

Read the story in the numbers:

- **d1 beats d3 despite identical `tf(rust) = 2`** — d3 is 3× longer, its norm
  (1.75) inflates the denominator, and its rust contribution drops from 0.71 to
  0.50. Length normalization is real and visible (V3's Done-when box).
- **d2, matching only one term, beats d3, which matches only one term too** —
  but d2 is *short* (norm 0.5 boosts it). Focus wins.
- **d1 wins overall** by matching both terms — coverage across query terms
  usually dominates, since each matched term adds a whole `idf·sat` block.

---

## 4. Top-k is a heap problem, not a sort problem

Scoring tells you each document's value; you still must *collect* the best `k`.
The trap the SPEC names: with 1M matching docs, `sort all, take 10` is
O(hits·log hits) time and O(hits) memory — you materialized a million scores to
throw away 999,990.

The fix: keep a **min-heap of size k** while streaming scores. The heap's root is
the *worst* of the current best-k — the bar to beat:

```
  scores stream in ──►  1M matching docs, one score each
                             │
                             ▼
                   ┌─ min-heap, capacity k=10 ─┐
                   │  root = 10th-best so far  │
                   │  new score ≤ root → DROP  │   O(1) for most docs
                   │  new score > root → pop   │   O(log k) occasionally
                   │  root, push new           │
                   └───────────────────────────┘
                             │ at end: pop all
                             ▼
                    top-10, worst→best (reverse for best-first)
```

Cost: O(hits · log k) time, O(k) memory — and since `k` is 10-ish and fixed,
that's effectively linear time and *constant* memory in the match-set size. This
is directly observable, which is why V3's Done-when phrases it as "memory/time
stay bounded in `k`, not in hits" and the bench plots top-k latency flat as the
match set grows. In Rust the shape is `BinaryHeap<Reverse<…>>` — with the wrinkle
that your score is an `f32`, which isn't `Ord`. How you make scores heap-ordered
(and how you accumulate per-document sums across query terms and segments before
they reach the heap) is the design work of [`Bm25::search`](../src/bm25.rs) — the
scaffold's TODO lays out the loop's *stations* (gather postings, skip tombstoned
docs via [`LiveDocs::is_live`](../src/merge.rs), score, bound to k, materialize
[`SearchHit`](../src/doc.rs)s), and the choices inside each are yours.

One scale note for honesty: real engines (Lucene's WAND/MaxScore) go further and
*skip scoring* documents that provably can't beat the heap's root. Out of scope
here — but that's the next rung on this ladder.

---

## 5. The design space you must walk through (not around)

1. **`k1` and `b`** — start at the Lucene defaults (1.2 / 0.75, already in
   [`Bm25Params::default`](../src/bm25.rs)), but the design doc wants *your*
   reasoning for *this* corpus. What's your test corpus's length distribution?
2. **Ordering `f32` scores in a heap** — total order over floats is yours to
   arrange (and NaN must be impossible upstream — see the edge cases).
3. **Per-document accumulation across terms and segments** — doc ids are
   per-segment; the same conceptual doc appears in exactly one segment, but hits
   from many segments flow into one heap. What's the accumulator keyed by?
4. **Where IDF's inputs are summed** — per segment or per shard? (The scaffold
   passes shard-wide [`CollectionStats`](../src/doc.rs) — note V5 will reveal
   what this choice costs across *shards*.)
5. **Don't trust your eyes** — a tiny corpus can't show stuffing or length bias;
   that's what the property tests are for. Eyeballing ranking on 5 docs is the
   trap the CONCEPTS card warns about.

When you've made the calls, `/hint` nudges a stuck spot; `/quest` runs the guided
build with the acceptance tests written before you implement.

---

## 6. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Ranking ≠ matching | Matching yields a set; the product is an ordering. |
| TF | Frequency in the doc signals aboutness — but must not pay linearly. |
| Saturation (`k1`) | `tf/(tf+k1·…)` caps repetition at `k1+1`; `k1=0` → presence-only; monotone but flattening. |
| Length norm (`b`) | TF judged per unit of length; `b=0` blind, `b=1` fully relative; lower it for uniform-length corpora. |
| IDF | Rarity is discrimination: 13.4 for a 1-in-a-million term, 0.11 for a 90% term. |
| Score | `Σ idf(t) · saturated, length-normalized tf` over query terms. |
| Top-k | Min-heap of size k over the score stream: O(hits·log k), O(k) memory — never sort the hits. |
| Edges | `avgdl = 0`, `doc_freq = 0`, tombstoned docs — none may panic, NaN, or score. |

## 7. Where you'll build this

- **Module:** [src/bm25.rs](../src/bm25.rs) — the `todo!()`s in `Bm25::score`
  and `Bm25::search`.
- **Unlocks these V3 Done-when boxes** ([SPEC.md](../SPEC.md)):
  - [ ] Results ranked best-first (frequent + focused beats rare + buried).
  - [ ] Score monotone in tf, and saturating.
  - [ ] Longer doc at equal tf scores lower (tunable via `b`).
  - [ ] Rarer term contributes more at equal tf.
  - [ ] Only top-k materialized — bounded in `k`, not in hits.
- **Proof artifacts:** `prop_score_monotone_in_tf`, the length-penalty property
  test, your own hand-computed corpus reproduced by the code, the flat top-k
  bench, and the `k1`/`b` rationale in `docs/20-design.md`.
