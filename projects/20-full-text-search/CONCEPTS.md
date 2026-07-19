# Concept Bank — Project 20: Full-Text Search Engine (Elasticsearch-lite)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — Analysis: text becomes terms, symmetrically *(V1 · `src/analyzer.rs`)*

**The problem.** `WHERE text LIKE '%rust%'` scans every row, can't rank, and thinks "Running" and "run" are strangers. Search isn't string matching — it's matching in a *derived* space: text is transformed into terms, and the transformation *defines what matches*. Which surfaces the silent killer: index `"Running"` as `running` but analyze the query `"Running"` differently, and the lookup misses everything. No error, no log line — just zero results and a user who thinks your data is gone.

**The idea.** The analyzer is a pipeline — tokenize → normalize (case-fold) → filter (stop-words, min-length) → optionally stem — and each stage is a recall/precision dial: lowercase buys recall (Rust finds rust) at a precision price (the language Polish vs polish furniture); stop-word removal shrinks the index but makes "The Who" unsearchable; stemming merges run/running/runs and occasionally merges things it shouldn't (university/universe, with a bad stemmer). The iron rule: **one analyzer, both paths** — index-time and query-time analysis must be the same code, symmetric by construction.

**In the wild:** Lucene analyzers (Elasticsearch's analysis chains), Postgres `tsvector`/`tsquery` configs, and every "why doesn't search find my document" ticket ever filed — which is almost always an analysis asymmetry.

**You own it when you can explain:**
- [ ] Why search operates on derived terms rather than raw text, and what that makes the analyzer (the definition of relevance's *vocabulary*).
- [ ] Each stage's recall/precision trade with a concrete query it helps and one it hurts.
- [ ] The asymmetry failure mode end to end: where the miss happens (dictionary lookup), why it's silent, and why "same code path" beats "same configuration" as the fix.
- [ ] Idempotence as a pipeline sanity check: re-analyzing analyzer output yields the same terms — what a non-idempotent stage would imply.
- [ ] Why analyzers are per-language in real systems (tokenization itself breaks on CJK; stemming is language-specific).

**Depth probes:**
- You change the stemmer on a live index. What happens to matches for already-indexed documents, and what does that force operationally (full reindex — why)?
- Exact-match fields (SKUs, emails) inside a full-text engine: what analyzer do they get (none/keyword), and why does running them through the text pipeline corrupt lookups?

**Trap:** "improving" the query-side normalization in a hotfix without reindexing. Every previously-indexed document is now on the far side of an asymmetry — the fix for bad matching just deleted your recall.

---

## 🧠 Card 2 — The inverted index, immutable segments & mmap *(V2 · `src/segment.rs`)*

**The problem.** Finding "rust" in a million documents by scanning is O(corpus) per query, forever. And whatever structure fixes that must simultaneously absorb writes and serve concurrent reads — a mutable-in-place index means locks on the read path, and a 10 GiB index means you can't just heap-load it.

**The idea.** Invert: map each term → sorted postings list (doc id, term frequency) — a query becomes a dictionary lookup + list walk, O(hits) not O(corpus). Writes buffer in memory; a **refresh** flushes the buffer into a brand-new **immutable segment** file; a shard is a pile of segments consulted together. Immutability is the concurrency design: readers need no locks against something that never changes, and merges (Card 4) can rewrite safely alongside live reads. **mmap** is the memory design: the reader binary-searches a sorted term dictionary and decodes postings *in the mapped bytes* — the OS page cache keeps hot terms in RAM and a cold 10 GiB segment costs address space, not heap.

**In the wild:** this is Lucene's architecture verbatim (segments, refresh, NRT search) — thus Elasticsearch and Solr; tantivy is the Rust telling; the immutable-files-plus-merge shape is shared with LSM engines (project 22).

**You own it when you can explain:**
- [ ] The complexity flip: what an inverted index makes cheap (term lookup) and what it makes expensive (updates — hence the whole segment design).
- [ ] The near-real-time contract: why a just-indexed doc is invisible until refresh, and what the refresh interval trades (searchability latency vs segment churn).
- [ ] Immutability's two dividends, precisely: lock-free concurrent reads, and safe background merging.
- [ ] What mmap actually does (a `&[u8]` view backed by the page cache) vs `read()` (copy into your heap) — and why resident memory stays far below segment size on cold data.
- [ ] The on-disk layout's job: sorted term dictionary you can binary-search *in place*, postings decodable without deserialization, a footer locating both — plus corruption detection on open.

**Depth probes:**
- What does the page cache give you for free that a hand-rolled block cache (project 22 V7) must build — and what control do you lose in exchange (eviction policy, memory accounting)?
- Postings compression (delta + varint): why do doc-id *gaps* compress so well, and what does that buy at a hundred million documents?

**Trap:** letting search consult the in-memory buffer "so docs are instantly visible". You've silently put a mutable structure on the lock-free read path — the NRT refresh contract exists precisely to keep mutation and search decoupled.

---

## 🧠 Card 3 — BM25: ranking, not just matching *(V3 · `src/bm25.rs`)*

**The problem.** A query matches 50,000 documents; matching is worthless without ordering. Naive TF-IDF has two failure modes users feel immediately: a document that repeats "rust" 100 times beats a genuinely relevant one (linear TF reward), and long documents beat short ones by sheer surface area. And a third, structural failure: sorting all 50,000 scored matches to return 10 is a heap of wasted work on every query.

**The idea.** BM25 = TF-IDF with two corrections. **Saturation** (`k1`): term-frequency contribution levels off — the 10th occurrence adds far less than the 1st, so keyword-stuffing stops paying. **Length normalization** (`b`): TF is judged relative to document length, so a focused paragraph beats a rambling page. Per query term: `idf(t) × saturated, length-normalized tf` — rare terms discriminate (high IDF), common ones barely count. And top-k is a *bounded min-heap* while scoring, never a sort: memory and time track k, not hit count.

**In the wild:** BM25 is the literal default in Lucene/Elasticsearch/OpenSearch/tantivy and the baseline every neural-retrieval paper still benchmarks against — 30 years old and embarrassingly hard to beat.

**You own it when you can explain:**
- [ ] Both TF-IDF failures with felt examples (keyword stuffing; the long-document win) and which parameter fixes each.
- [ ] The saturation curve's shape and what `k1` controls (how fast repetition stops mattering) — plus what `k1=0` degenerates to (pure IDF presence).
- [ ] What `b` interpolates between (`b=0`: length-blind; `b=1`: fully normalized) and a corpus where you'd lower it (uniform-length tweets).
- [ ] IDF's role as the discrimination weight: why "the" contributes ~nothing at any TF.
- [ ] The top-k heap argument: why a min-heap of size k over a stream of scores is O(hits · log k) and O(k) memory — and why "sort the results" betrays the whole design at scale.

**Depth probes:**
- Compute BM25 by hand for a 2-term query over a 3-doc corpus — the SPEC's hand-check exists because doing it once makes the formula yours.
- Where does BM25 stop being enough (semantic similarity, synonyms) and what do hybrid systems bolt on (embeddings + BM25 fusion)?

**Trap:** validating ranking by eyeball on a tiny corpus. The pathologies BM25 fixes (stuffing, length bias) only *express* on realistic distributions — property-test the monotonicity and saturation instead.

---

## 🧠 Card 4 — Merging & tombstone deletes *(V4 · `src/merge.rs`)*

**The problem.** Every refresh mints a segment; a busy shard drifts toward hundreds. Each query consults *every* segment, so search latency degrades with write traffic — the index slowly poisons itself. And deletes seem impossible: the segments are immutable — you can't remove a document from a file you've sworn never to modify.

**The idea.** LSM thinking (project 22's compaction, in search clothing). **Merge**: rewrite N small segments into one larger immutable segment, retire the inputs — reads stay lock-free throughout because nothing live is mutated; the swap is atomic. **Deletes**: a tombstone overlay (`LiveDocs`) marks documents dead; queries consult it instantly (deletes are visible now), but bytes are reclaimed only when a merge rewrites without the dead docs. Cheap instant delete, deferred physical reclaim. The merge *policy* (trigger, tiering) is a deliberate dial between merge I/O (write amplification) and query cost (segment count).

**In the wild:** Lucene's merge scheduler and per-segment `liveDocs` bitsets — `forceMerge` is the Elasticsearch op you now understand; the identical pattern runs in RocksDB compaction and Postgres VACUUM (dead tuples = tombstones, VACUUM = the merge).

**You own it when you can explain:**
- [ ] The segment-count → search-latency mechanism (per-segment dictionary lookups, result union) and why unmerged shards degrade *without any data growth*.
- [ ] Why immutability forces the tombstone design, and the two-timescale delete story: logically instant, physically deferred.
- [ ] The merge-correctness contract: union of live postings, nothing lost, nothing resurrected — and what "resurrected" would mean (a merge that ignores tombstones).
- [ ] The policy space: merge-on-every-write (max write amplification) vs never (max read cost) vs tiered triggers — and what `merge_factor` tunes.
- [ ] Why a merge must be *invisible* to correctness — results identical before and after, only layout and speed change.

**Depth probes:**
- Write amplification arithmetic: a document that lives through 4 merge generations is written how many times? What does that cost on SSDs?
- Why does Postgres VACUUM map onto this exactly (MVCC dead tuples, visibility maps) — what's the shared root cause (never update in place)?

**Trap:** merging eagerly "to keep things tidy". Every merge re-writes live data; over-merging burns I/O the queries never get back. The policy is a budget, and the segment-count metric is how you know it's balanced.

---

## 🧠 Card 5 — Scatter-gather across shards *(V5 · `src/shard.rs`)*

**The problem.** One index tops out one core and one heap. Split the corpus across N independent shards and every query becomes a distributed operation: it must consult all shards (the winner could be anywhere), merge their answers, and return one ranked list — now the query is as slow as the *slowest* shard, and a subtle correctness crack opens: BM25's IDF is a *collection-wide* statistic each shard computes only for itself.

**The idea.** Route each document to exactly one shard (stable hash of id; keyless spread). A search fans out **concurrently** (a sequential loop makes latency the *sum* of shards — the one-word bug), takes each shard's local top-k (the global top-k is provably within the union of local top-ks), and merges. The tail-latency lesson: p(all N fast) = p(fast)^N, so the gather's p99 is dominated by the worst shard — you fight variance, not means. The scoring caveat is stated honestly: per-shard IDF skews cross-shard comparability; balanced shards keep it small; a two-phase query (gather global stats first) fixes it at a round-trip's cost.

**In the wild:** Elasticsearch's query-then-fetch *is* this (with `dfs_query_then_fetch` as the two-phase fix); the fan-out/tail-latency struggle is Google's "The Tail at Scale" paper; every distributed OLAP engine gathers this way.

**You own it when you can explain:**
- [ ] Why local top-k from each shard suffices for a correct global top-k (per-shard scores don't change; the winner is in someone's top-k).
- [ ] The tail math: 20 shards each 99%-fast-under-50ms gives what overall p(fast)? Why hedged/partial requests exist.
- [ ] Concurrent vs sequential fan-out as an observable property: latency ~flat vs linear in shard count for a fixed corpus.
- [ ] The IDF skew: construct the two-shard case where the same document scores differently by address — then say why balanced routing keeps it tolerable and what two-phase does instead.
- [ ] Stable routing's role (same id → same shard) for updates/deletes to find their document.

**Depth probes:**
- Per-shard timeouts returning partial results: what does the response need to admit ("results from 19/20 shards"), and when is that the right product call?
- Why does adding shards eventually *hurt* a fixed-size corpus (per-shard overhead, more tails to lose) — what sets the sweet spot?

**Trap:** requesting only k/N results from each shard "since they'll merge to k". The global top-k can be entirely inside one shard — each must return its full local top-k, or correctness leaks quietly on skewed data.

---

## ⚡ Rapid-fire round

- [ ] The query cache: keyed on what, invalidated by refresh/merge/delete — and why a stale hit (returning a deleted doc) is worse than a miss.
- [ ] Why a Zipfian query mix is what makes caching pay (hot head amortizes; long tail keeps the engine honest).
- [ ] The contention model, stated deliberately: lock-free segment reads, serialized buffer writes.
- [ ] Input caps as query-cost control: `MAX_QUERY_TERMS` bounds fan-out, `MAX_DOC_BYTES` bounds indexing cost.
- [ ] The health gauge: live segment count per shard — the "am I merging enough" number.
- [ ] Graceful shutdown: final refresh or an explicitly stated "un-refreshed docs are not durable" contract — never an implicit maybe.

## 🔗 Connects to

- Immutable-files-plus-merge is project 22's LSM shape — build both and you'll never confuse "how Lucene works" with "how RocksDB works" again (they're siblings).
- The mmap/page-cache lesson pairs with project 22's hand-built block cache — the two ways to manage read memory.
- Scatter-gather's tail-latency fight is project 07's ring plus project 10's load balancing, felt from the query side.
