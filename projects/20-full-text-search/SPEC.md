<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 20 — Full-Text Search Engine (Elasticsearch-lite)

> `WHERE text LIKE '%rust%'` works until it doesn't: it scans every row, can't rank,
> and ignores that "Running" should match "run". A search engine is a different data
> structure entirely — an **inverted index** that maps each word to the documents
> containing it, so a query is a dictionary lookup and a list walk instead of a scan.
> Elasticsearch/Lucene wrap that core in analysis, BM25 relevance, immutable
> segments, background merging, and sharded fan-out — and every one of those exists
> to keep search *fast and relevant as the corpus grows past what one scan, one core,
> or one machine can handle*. This project builds that core from scratch. It's Tier 7
> because it spans all four pillars at once: on-disk data structures (segments +
> mmap), ranking math (BM25), caching (the query cache), and distributed systems
> (scatter-gather across shards) — the parts a `cargo add tantivy` would hide.

## What it does (the easy part)
- `POST /documents` `{id?, text}` → index a document; returns its `(shard, doc_id)`.
- `POST /_bulk` (NDJSON, one document per line) → index a batch.
- `POST /_refresh` → flush buffered documents into segments so they become searchable.
- `GET /search?q=&size=` → the top-`size` documents for a query, ranked by relevance,
  with the time it took.
- `DELETE /documents/{id}` → tombstone a document.
- `POST /_forcemerge` → compact each shard to one segment, dropping tombstoned docs.
- `GET /_stats` → per-shard segment/document counts.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The analyzer — *text → terms, symmetrically*
Before anything can be indexed or searched, text must become **terms**. The analyzer
is the pipeline that does it: tokenize (split on word boundaries), normalize
(lowercase / case-fold), filter (drop stop-words and too-short tokens), optionally
stem (`running` → `run`). Whatever it does *defines what matches* — if it lowercases,
`Rust` finds `rust`; if it strips stop-words, a search for `the` finds nothing. Build
it in `src/analyzer.rs`.

The trap is asymmetry. The **same analyzer must run at index time and query time**.
Index `"Running"` as `running` but analyze the query as `Running`, and the lookup
misses every document, silently. This project shares one analyzer between both paths
so it's symmetric by construction — your job is to make the pipeline itself correct.

**Done when ALL true:**
- [ ] A document and a query that a human would call a match produce an **overlapping term set** (case, punctuation, and — if enabled — stop-words don't break the match).
- [ ] Analysis is **idempotent**: re-analyzing the joined output of the analyzer yields the same terms (it's a fixed point on its own output).
- [ ] The **same analyzer** is provably used for indexing and querying — not two code paths that can drift.
- [ ] Each pipeline stage (lowercasing, stop-words, min-length, any stemming) is a **deliberate, documented** choice, and its effect is observable (toggling it changes which queries match).

**Proof:** unit tests for a fixed input → expected term stream; a property test for
idempotence (`prop_analyze_is_idempotent`); a match test showing an indexed doc and a
query analyze to overlapping terms; `docs/20-design.md` states the analysis contract.

*Concept to internalize:* why analysis, not string matching, is what search is; the
recall-vs-precision tradeoff each filter makes; and why index-time == query-time
symmetry is non-negotiable.
**Stretch:** a stemmer (Porter) or per-field analyzers.

### V2. The inverted index & on-disk segments — *mmap, don't scan*
The heart of the engine. Build the **inverted index** — `term → sorted postings
(doc id + term frequency)` — and make it live on disk as **immutable segments**.
Newly indexed documents buffer in memory; a *refresh* flushes them into a brand-new
segment file that is never modified again; a shard is an ordered pile of segments. A
[`SegmentReader`] answers a query by `mmap`ing its file and parsing postings straight
out of the mapped bytes — the OS page cache serves hot terms, and a 10 GiB segment
never has to sit in the heap. Build it in `src/segment.rs`.

The two ideas doing the work: **immutability** (concurrent search needs no locks;
merging is safe) and **mmap** (you read a `&[u8]` view of the file, not the whole file
into RAM). The trap is the on-disk format — a sorted term dictionary you can
binary-search in the mapped bytes, postings you can decode in place, and a footer that
tells the reader where everything is.

**Done when ALL true:**
- [ ] After flushing documents to a segment and reopening it, `postings(term)` returns **exactly the documents that contained the term**, doc-id-sorted, with the right term frequencies.
- [ ] A search **finds a document only after a refresh** — a just-indexed, un-refreshed document is not yet searchable (the near-real-time contract), and this is documented.
- [ ] Segments are **immutable**: indexing more documents creates *new* segment files and never rewrites an existing one (observable as growing file count).
- [ ] A query is answered **without reading the whole segment into memory** — postings are read from the `mmap` (resident set stays far below segment size for a cold, large segment).
- [ ] A **truncated or byte-flipped segment** is detected on open/read and surfaces as an error, never as wrong postings.

**Proof:** flush→reopen→`postings` round-trip tests; a test asserting a doc is
invisible until refresh, then visible; a corruption test; a `bench/` number for
postings-lookup latency as a segment grows (flat, not linear). `docs/20-design.md`
documents the on-disk segment format.

*Concept to internalize:* why the inverted index turns O(corpus) search into O(hits);
why immutable segments make concurrency and merging tractable; and what `mmap` + the
page cache actually buy you over `read()`.
**Stretch:** delta-encode + variable-byte compress the postings; store positions to
enable phrase queries.

### V3. BM25 ranking — *relevance, not just matching*
Matching returns a set; ranking is what makes search useful. Implement **BM25**, the
scoring function Lucene/Elasticsearch actually use, and return the top-`k` by score.
BM25 refines TF-IDF with two corrections: **term-frequency saturation** (`k1` — the
10th occurrence counts far less than the 1st) and **document-length normalization**
(`b` — a long document shouldn't win by sheer size). A document's score is the sum,
over the query's terms, of `idf(t) · saturated, length-normalized tf`. Build it in
`src/bm25.rs`.

The trap is the top-`k`: with a million matching documents you must **not** sort them
all — keep only the best `k` with a bounded heap as you score.

**Done when ALL true:**
- [ ] Results are **ranked**, best-first: a document where the query terms are frequent and the document is focused ranks above one where they're rare and buried.
- [ ] Score is **monotonic in term frequency** (more occurrences never lowers the score) and **saturating** (it levels off — not linear in TF).
- [ ] A **longer** document with the same term frequency scores **lower** than a short one (length normalization is real, and tunable via `b`).
- [ ] A term appearing in **fewer documents** (higher IDF) contributes more than a common one at equal TF.
- [ ] Only the **top `k`** are materialized — the full matching set is never sorted (verifiable on a large corpus by memory/time staying bounded in `k`, not in hits).

**Proof:** property tests for TF-monotonicity (`prop_score_monotone_in_tf`) and the
length penalty; a hand-computed tiny-corpus ranking the code reproduces; a `bench/`
showing top-k latency independent of the match-set size. `docs/20-design.md` records
the `k1`/`b` choice and why.

*Concept to internalize:* why raw TF-IDF over-rewards repetition and long documents,
what `k1`/`b` actually do, and why top-k is a heap problem, not a sort problem.
**Stretch:** boolean/phrase queries; per-field boosting.

### V4. Segment merging & deletes — *keep search fast, reclaim space*
Every refresh writes a segment, so a busy shard drifts toward hundreds of tiny
segments — and a query pays to consult all of them, so search slows as the count
climbs. **Merging** compacts many small segments into one larger immutable segment and
retires the inputs (the same idea as LSM compaction). **Deletes** ride along: since
segments are immutable, a delete records a **tombstone** in a [`LiveDocs`] overlay that
search skips; the space is only reclaimed when a merge rewrites the segment and drops
the dead docs. Build it in `src/merge.rs`.

**Done when ALL true:**
- [ ] Merging N segments yields **one segment whose live postings equal the union of the inputs'** — nothing lost, nothing resurrected, ordering preserved.
- [ ] A **deleted document disappears from results immediately** (the tombstone is consulted at query time), *before* any merge runs.
- [ ] A merge **physically drops** tombstoned documents — after a force-merge the shard holds one segment and its size reflects only live docs.
- [ ] The **merge policy is a deliberate, documented** trigger (e.g. more than `merge_factor` segments), balancing merge I/O against search cost — not merge-on-every-write, not never.
- [ ] Search results are **unchanged across a merge** (a merge is transparent to correctness — it only changes layout and speed).

**Proof:** a property test that a merge preserves exactly the live postings
(`prop_merge_preserves_live_docs`); a delete-then-search test (gone immediately); a
force-merge test (one segment, results identical); `docs/20-design.md` states the
merge policy and the delete/tombstone model.

*Concept to internalize:* the write-amplification tradeoff (cheap instant deletes,
deferred reclamation); why immutable-segments-plus-merge beats in-place mutation; and
how segment count drives search latency.
**Stretch:** tiered vs. leveled merge policies; persist tombstones so deletes survive
a restart.

### V5. Scatter-gather across shards — *partition, fan out, merge*
One index on one core tops out. **Shard** the corpus across N independent indexes,
route each document to one shard, and turn a search into a **scatter-gather**: fan the
query out to every shard *concurrently*, take each shard's local top-`k`, and merge
them into a global top-`k`. Build the coordinator in `src/shard.rs`.

Three subtleties are the whole point: (1) you only need `k` from each shard (the global
winner is in some shard's top-k); (2) a gather is only as fast as the **slowest shard**
— tail latency, not average, is what you fight; (3) BM25's IDF uses *collection* stats,
and each shard knows only its own, so **scores aren't strictly comparable across
shards** — a real caveat this lite engine accepts (balanced shards keep it close) and
that a two-phase query would fix.

**Done when ALL true:**
- [ ] A document is routed to **exactly one shard**, and the same client id always lands in the same shard (stable routing); keyless docs spread across shards.
- [ ] A search **consults every shard** and returns a correct global top-`k` — a document in any shard can appear in the results.
- [ ] Shards are queried **concurrently**, not in a sequential loop — total search time tracks the *slowest* shard, not the *sum* of shards (observable as latency staying ~flat as shard count rises for a fixed corpus).
- [ ] The **cross-shard scoring caveat** (per-shard IDF) is documented, with the accepted tradeoff or the two-phase fix stated.

**Proof:** a routing test (id→shard stability + keyless spread); a correctness test (a
doc in each shard all appear in a global search); a `bench/` showing search latency vs.
shard count (parallel fan-out, not linear). `docs/20-design.md` covers the routing
function and the global-IDF tradeoff.

*Concept to internalize:* why sharding buys throughput, why tail latency dominates a
gather, and why distributed scoring needs shared collection statistics.
**Stretch:** per-shard timeouts returning partial results; a two-phase query that
gathers global term stats first so scores compare.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Bulk indexing:** `_bulk` accepts many documents in one request (NDJSON); a
  single search returns a **bounded** result set (`size` cap) — a query can never be
  asked to return the whole corpus.
- [ ] **Refresh semantics documented:** the near-real-time contract (a doc is
  searchable only after a refresh) is written down, and the refresh interval is a
  documented latency-vs-throughput knob.
- [ ] **Graceful shutdown** drains in-flight requests and flushes buffered documents
  (a final refresh) so a clean shutdown doesn't silently drop un-refreshed docs — or
  the "un-refreshed docs are not durable" contract is stated explicitly.

### Caching
- [ ] **Query cache:** a repeated query is served from the cache without touching any
  shard (a cache hit does zero scoring) — `src/cache.rs`.
- [ ] **Invalidation is correct:** a refresh, merge, or delete invalidates cached
  results so search never returns a stale hit for a document that changed.
- [ ] The **cache policy is documented** in `docs/20-design.md` (what's keyed, the
  eviction rule, and the invalidation trigger). *(Stretch: single-flight — coalesce
  concurrent misses on the same hot query so one search does the work.)*

### Security
- [ ] **Auth on write/admin routes** (`/documents`, `/_bulk`, `DELETE`, `/_refresh`,
  `/_forcemerge`): a request without a valid API key is rejected before the handler
  runs; search is public; keys never appear in logs or errors.
- [ ] **Input validation:** a document over `MAX_DOC_BYTES` and a query with more than
  `MAX_QUERY_TERMS` analyzed terms are rejected (a pathological query can't fan out
  into a scan of everything); external ids are length/charset-validated. Each with a test.

### Observability
- [ ] `tracing` span per request (via `common-telemetry`), with a request id.
- [ ] Structured logs on the events that matter: **segment flush (refresh)**, **merge**,
  and **delete**.
- [ ] Metrics at `/metrics`: **searches/sec**, a **search-latency histogram** (source
  of p99), **query-cache hit ratio**, **live segment count per shard**, **documents
  indexed**, and **merges** — the segment-count gauge is *the* "am I merging enough"
  signal.

### Cross-cutting scale skills
- [ ] Concurrent searches read the **immutable segments lock-free**; indexing serializes
  into the in-memory buffer — the contention model is deliberate, not incidental.
- [ ] The `bench/` reports **postings-lookup and search latency vs. corpus size** (flat,
  thanks to the index) and **search latency vs. shard count** (parallel fan-out).

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/20-benchmarks.md`.
3. `docs/20-design.md` records the five decisions the SPEC grades: the **analysis
   contract** (V1), the **on-disk segment format + mmap read path** (V2), the **BM25
   `k1`/`b` choice** (V3), the **merge policy + delete model** (V4), and the **sharding
   function + cross-shard scoring tradeoff** (V5) — plus the **query-cache policy**.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p full-text-search` are
   green; no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Long Tail

> Your index has grown to a million documents and is still being written to. The
> query traffic is Zipfian: a handful of queries are white-hot, and a long tail are
> each seen once. Every weakness shows up here at once — a naive engine scans the
> corpus, an un-merged shard drowns in segments, a sequential fan-out is only as fast
> as its slowest shard, and the hot queries re-score from scratch every time. The Long
> Tail is the query mix that finds all four.

**Arena:** `bench/` load test (`oha` or `k6`) against a **release build**
(`cargo run --release`), with a corpus of **≥ 1,000,000 documents** pre-indexed and a
background writer continuously indexing + refreshing during the run. Queries follow a
Zipfian distribution (a small hot set + a long unique tail). Two runs: query-cache on
vs. off.

**The boss falls when ALL true:**
- [ ] ≥ **2,000 searches/sec** sustained for 60s over the ≥ 1M-document corpus.
- [ ] **p99 ≤ 50ms** during that run (the scatter-gather tail included).
- [ ] Query-cache **hit ratio ≥ 80%** on the Zipfian mix, and the cache-on run beats
  cache-off by **≥ 3×** throughput on the hot set.
- [ ] **Live segments per shard stay bounded** (≤ 20) under continuous indexing —
  merges keep pace, so search latency doesn't drift upward over the run.
- [ ] Top-`k` results **match a brute-force reference** for a sample of queries — no
  silently wrong ranking under load.

**Proof:** methodology + before/after numbers in `docs/20-benchmarks.md` (hardware
noted, corpus + query generator and commands reproducible via `bench/`).

## Suggested order of attack
1. Boring path: index into an in-memory `HashMap<Term, Vec<(DocId, tf)>>`, score with
   a naive loop, no disk, one shard. Prove index → refresh → search round-trips and
   ranks sanely.
2. **V1:** the analyzer — make index-time and query-time analysis identical.
3. **V2:** the on-disk segment — flush the buffer to an immutable file, read postings
   back via `mmap`; search consults segments, not the buffer.
4. **V3:** BM25 top-`k` with a bounded heap.
5. **V4:** segment merging + tombstone deletes; then a merge policy.
6. **V5:** shard the corpus and make search a concurrent scatter-gather + merge.
7. The query cache, auth + validation, and the metrics (searches/sec, latency
   histogram, segment count, cache hit ratio).
8. Benchmark, document, tune.

## Run it
```bash
cp .env.example .env        # then adjust INDEX_DIR / SHARD_COUNT if you like
cargo run -p full-text-search   # no external deps — the filesystem IS the index
```

[`SegmentReader`]: src/segment.rs
[`LiveDocs`]: src/merge.rs
