# Scatter-Gather Across Shards — From First Principles

> A ground-up guide to making search a distributed operation: partitioning a
> corpus across independent indexes, fanning a query out to all of them at once,
> and merging their answers — plus the two things that bite everyone who does
> this (tail latency and non-comparable scores). No distributed-systems
> background assumed. Prepares you for **V5** in [SPEC.md](../SPEC.md): the
> `todo!()` in [`ShardedIndex::scatter_gather`](../src/shard.rs) is what you're
> about to build. Anchored to [shard.rs](../src/shard.rs),
> [index.rs](../src/index.rs), and [doc.rs](../src/doc.rs). The probability and
> IDF numbers below are machine-computed.

---

## 0. The one sentence to hold onto

**Split the corpus into N independent indexes, send every query to all of them
*at the same time*, and merge their local top-k lists — after which your latency
is set by the slowest shard and your scores are quietly no longer comparable.**

Both halves of that sentence matter. The first is the easy win; the second is
the pair of costs V5 exists to make you feel.

---

## 1. The problem: one index tops out

One shard is one in-memory buffer taking serialized writes, one pile of segments
on one disk, one query execution touching it all. Growth breaks it on every axis:

| One big index | What breaks |
| --- | --- |
| Indexing serializes into one buffer | Write throughput caps at one writer's pace, no matter how many cores idle |
| One query walks all postings alone | A query over a 100M-doc index uses one core while fifteen watch |
| One disk, one page cache | The working set outgrows one machine's RAM; postings for hot terms fight each other for cache |
| One `avgdl`, one everything | Fine — until the data simply doesn't fit |

The fix is the same one as every storage system in this repo: **partition**.
Split the corpus into `N` shards — each a complete, independent
[`Index`](../src/index.rs) with its own buffer, segments, tombstones, and stats
(look at [`ShardedIndex::open`](../src/shard.rs): it literally builds N `Index`
instances under `shard-0/`, `shard-1/`, …). Writes spread across N buffers;
a query runs on N cores; N page caches hold N working sets.

But now *every* search is a distributed query, because a matching document could
live in **any** shard. Enter scatter-gather.

---

## 2. Routing: every document lives in exactly one shard

First, writes. A document must land in **exactly one** shard — and, crucially,
a document with a client id must land in a *predictable* one, or
`DELETE /documents/{id}` can never find it again. The scaffold's
[`route`](../src/shard.rs) is already implemented — read it:

```rust
match id {
    Some(id) => hash(id) % n,                      // stable: same id → same shard, forever
    None     => round_robin_counter % n,            // keyless: spread evenly
}
```

Stable hashing gives the two properties V5's first Done-when box names: the same
external id always resolves to the same shard (so delete and re-index find their
document), and keyless documents spread uniformly (so shards stay *balanced* —
remember that word, it's load-bearing in §5). This is the simplest member of the
partitioning family you met in project 07 (consistent hashing); with a fixed
shard count for the process lifetime, plain modulo is honest and sufficient —
one more thing to *say* in the design doc rather than leave implicit.

---

## 3. Scatter-gather: the query plan

A search now has three acts, and
[`scatter_gather`](../src/shard.rs) — your `todo!()` — is all of them:

```
                        query terms (analyzed ONCE, at the coordinator)
                                        │
              ┌───────────┬─────────────┼─────────────┬───────────┐
              ▼           ▼             ▼             ▼           ▼      SCATTER
         shard 0      shard 1       shard 2       shard 3     shard 4    (all at once)
         local        local         local         local       local
         top-k        top-k         top-k         top-k       top-k
              └───────────┴─────────────┼─────────────┴───────────┘
                                        ▼                                GATHER
                          merge 5 sorted lists of ≤ k
                                        ▼
                              global top-k (truncate to k)
```

Three facts carry the design:

**(a) k from each shard suffices.** Why not ask each shard for *everything* it
matched? Because you don't need it: a shard's local scores don't change based on
what other shards hold, so any document in the *global* top-k must already be in
its *own shard's* top-k — a doc that can't crack the top-k of its own shard
can't crack the global one either. So each shard returns at most `k` hits and
the coordinator merges N short sorted lists. Beware the tempting "optimization"
the CONCEPTS card flags: asking each shard for only `k/N` hits. Skewed data can
put the entire global top-k inside **one** shard — each shard must return its
full local top-k, or correctness leaks silently.

**(b) The fan-out must be concurrent — the one-word bug.** Query the shards in a
`for` loop with `.await` inside and each shard waits for the previous one:
latency = **sum** of shard times. Launch them all, then await them together:
latency = **max** of shard times.

```
   sequential:  |──s0──|──s1──|──s2──|──s3──|      total = Σ  (grows with N)
   concurrent:  |──s0──|
                |───s1───|
                |──s2──|
                |──s3─|                            total = max (≈ flat in N)
```

This is directly observable, which is why V5's Done-when phrases it as an
observable: search latency stays ~flat as shard count rises for a fixed corpus.
The scaffold's TODO points at the tools (`tokio::spawn` over `Arc<Index>`, or
joining a set of futures); the shape of the concurrency is your call.

**(c) The gather is a k-way merge.** N sorted-by-score lists of ≤ k hits merge
into one — the same discipline as V4's dictionary merge and V3's bounded
collection, at friendlier scale (N·k is tiny). Each
[`SearchHit`](../src/doc.rs) already carries its `shard` tag, because
[`DocId`](../src/doc.rs)s are only unique *within* a shard — two shards both
have a doc 0, and only `(shard, doc_id)` names a document globally.

---

## 4. The tail: your gather is as slow as your slowest shard

Here's the cost hiding inside "latency = max of shard times." Each shard is
*usually* fast — but a gather needs **all** shards to be fast **at once**, and
probability is merciless about "all". If each shard independently answers under
50 ms 99% of the time:

| Shards | P(all fast) = 0.99^N | Queries that eat the tail |
| --- | --- | --- |
| 1 | 99.0% | 1.0% |
| 5 | 95.1% | 4.9% |
| 10 | 90.4% | 9.6% |
| 20 | 81.8% | **18.2%** |
| 50 | 60.5% | 39.5% |

Read the 20-shard row again: per-shard p99 of 50 ms turned into nearly **one in
five** user queries waiting on some shard's slow path. Your per-shard *median*
barely matters; the gather samples the *worst* of N draws every single time. To
get 98% of 20-shard gathers fast, each shard needs to be fast 99.9% of the time
(0.999²⁰ ≈ 0.980) — you must fight **variance**, not means. This is the thesis
of Google's "The Tail at Scale," and it's why the boss fight's p99 target says
"the scatter-gather tail included."

What actually causes a shard's slow 1%? Everything from this project's earlier
verticals: a merge hogging I/O (V4), a cold mmap faulting pages in (V2), an
unlucky allocation, the OS scheduling someone else. You can't eliminate the
tail; systems *manage* it — per-shard timeouts that return partial results
("hits from 19/20 shards", honestly admitted in the response), or hedged
requests where a replica races the laggard. Both are V5 stretches; the required
part is knowing *why* they exist.

---

## 5. The crack: BM25 scores stop being comparable

The subtle one. Recall from V3 that a hit's score includes `idf(t) =
ln(1 + (N − n + 0.5)/(n + 0.5))` — computed from **collection** statistics. But
each shard passes its *own* [`CollectionStats`](../src/doc.rs) to its own
scorer: its `N`, its `n(t)`, its `avgdl`. The coordinator then merges scores
computed against **different rulers**.

Construct the failure (machine-computed): 1M docs split evenly, but the term
`zig` clusters — 990 of its 1,000 occurrences landed in shard A:

| | shard A | shard B | true global |
| --- | --- | --- | --- |
| docs (N) | 500,000 | 500,000 | 1,000,000 |
| docs containing `zig` (n) | 990 | 10 | 1,000 |
| idf(`zig`) | **6.22** | **10.77** | 6.91 |

Identical documents — same tf, same length — score **1.73× higher** for `zig`
in shard B than in shard A, purely because of which shard they were routed to.
The gather's k-way merge then ranks B's mediocre matches above A's good ones.
No error, no crash — just quietly unfair ranking.

Why ship it anyway? Look at what made the example possible: *the term clustered*.
Random routing (hash of id, round-robin) makes each shard a uniform sample of
the corpus, so per-shard `n(t)/N` concentrates near the global ratio and the
skew stays small. That's the real reason §2's "balanced shards" matters — it's
not just about even load, it's what keeps per-shard IDF honest. The proper fix
is a **two-phase query**: phase 1 gathers every shard's `(n(t), N, total_length)`
for the query's terms; phase 2 scores with the summed *global* stats. Cost: an
extra round-trip per query. Elasticsearch ships both — default
`query_then_fetch` accepts the skew exactly as this project does, and
`dfs_query_then_fetch` is the two-phase fix, off by default because balanced
shards keep the error below what the round-trip costs.

This engine accepts the tradeoff — **stating it in `docs/20-design.md` is
itself a Done-when box.** Knowing precisely what corner you cut is the skill.

---

## 6. The design space you must walk through (not around)

1. **The concurrency mechanism** — `tokio::spawn` + join handles, or
   `join_all` over futures? What happens to the gather if one shard's task
   panics or errors — fail the query, or degrade?
2. **The merge** — N sorted lists into one top-k: repeated selection, a heap,
   or sort-the-concatenation (N·k is small — what's actually worth it here)?
3. **Ordering guarantees** — ties across shards: does the result order need to
   be deterministic? (Your brute-force-reference boss-fight check will care.)
4. **Stretch: partial results** — a per-shard timeout needs a response shape
   that admits incompleteness. What does the API contract say?
5. **Stretch: two-phase scoring** — where would global stats gathering slot
   into the existing `search → scatter_gather` flow without doubling latency
   for the common case?

`/hint` for a nudge; `/quest` for the guided build with acceptance tests first.

---

## 7. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Shard | A complete independent index; N of them spread writes, cores, and cache. |
| Routing | Stable hash of client id (same id → same shard, forever); round-robin for keyless. Balance is load *and* scoring fairness. |
| Scatter | Fan out to all N concurrently — sequential await makes latency Σ instead of max. |
| Local top-k suffices | The global winner is in some shard's own top-k; never request k/N. |
| Gather | K-way merge of N sorted ≤ k lists; `(shard, doc_id)` is the global identity. |
| Tail math | P(all fast) = p^N: 20 shards at 99% each → 18.2% of gathers eat the tail. Fight variance. |
| IDF skew | Per-shard collection stats = different rulers; clustered terms mis-rank (6.22 vs 10.77). Balanced routing shrinks it; two-phase fixes it. |
| Honest tradeoffs | Accepted skew + documented caveat beats silent wrongness — write it down. |

## 8. Where you'll build this

- **Module:** [src/shard.rs](../src/shard.rs) — the `todo!()` in
  `ShardedIndex::scatter_gather` (routing, config, and the coordinator shell
  are already implemented — read them first; the per-shard `search_local` lives
  in [index.rs](../src/index.rs)).
- **Unlocks these V5 Done-when boxes** ([SPEC.md](../SPEC.md)):
  - [ ] A document routes to exactly one shard, stably; keyless docs spread.
  - [ ] A search consults every shard; any shard's doc can surface globally.
  - [ ] Shards are queried concurrently — latency tracks the slowest shard, not the sum.
  - [ ] The cross-shard scoring caveat is documented, with the tradeoff or two-phase fix stated.
- **Proof artifacts:** the routing stability/spread test, the
  one-doc-per-shard-all-appear test, the latency-vs-shard-count bench (flat, not
  linear), and the routing function + global-IDF tradeoff in `docs/20-design.md`.
