# Segment Merging & Tombstone Deletes — From First Principles

> A ground-up guide to the maintenance half of a segmented index: why an
> untended shard slowly poisons itself, how immutable files can possibly support
> deletes, and why "merge" is a budget you tune rather than a chore you run. No
> LSM/compaction background assumed (though if you've met project 22, this is the
> same idea in search clothing). Prepares you for **V4** in [SPEC.md](../SPEC.md):
> the `todo!()`s in [`MergePolicy::plan`](../src/merge.rs) and
> [`merge`](../src/merge.rs) are what you're about to build. Anchored to
> [merge.rs](../src/merge.rs) and [segment.rs](../src/segment.rs).

---

## 0. The one sentence to hold onto

**Immutability made writes and concurrency easy by pushing the hard work into
the future — merging is the future arriving, and tombstones are how deletes
wait for it.**

---

## 1. Problem 1: the index poisons itself

V2's design mints a new segment on **every refresh**. Refresh every second under
steady writes and the arithmetic is grim: one hour → 3,600 segments in a shard.
Nothing is *wrong* — every segment is valid — but watch what a single query now
costs. A query must consult **every** segment (a matching doc could be in any of
them):

```
   query "rust fast"  →  for EACH of S segments:
                            binary-search its term dict   (per term)
                            decode + walk its postings
                         then union S partial result sets
```

Per-segment work has a floor — dictionary probes, page faults on cold mappings,
per-segment bookkeeping — so query cost grows roughly **linearly in S**, even
when total data hasn't grown at all:

| Shard state | Same 1M docs, same query | What the user sees |
| --- | --- | --- |
| 5 segments | 5 dictionary lookups per term, 5 unions | fast |
| 3,600 segments | 3,600 dictionary lookups per term | search latency drifts up hour by hour |
| …and forever | every future query pays it | "search got slow" with **zero data growth** |

This is the sneaky part: the degradation is a function of **write traffic**, not
corpus size. A busy shard degrades while an idle one stays crisp. The boss fight
targets this directly: "live segments per shard stay bounded (≤ 20) under
continuous indexing" — and the segment-count gauge in your metrics is *the*
"am I merging enough" signal.

**The fix — merge:** take N small segments, write **one** new immutable segment
containing everything live in them, atomically swap it in, delete the inputs.
Reads stay lock-free the whole time — that's immutability's second dividend from
V2 paying out: the inputs never change while you read them, the output isn't
visible until complete, and searches holding `Arc<SegmentReader>`s to the old
segments simply finish on the old view before the readers are retired.

```
   before:   [seg-0][seg-1][seg-2][seg-3][seg-4][seg-5][seg-6]   S = 7
                 └──────┴──────┴──────┘
                     merge (reads inputs, writes seg-7)          ← searches
   after:    [seg-7................][seg-4][seg-5][seg-6]        S = 4
             inputs deleted once no search still holds them
```

---

## 2. Problem 2: deleting from a file you swore never to modify

`DELETE /documents/{id}` arrives. The document's postings are baked into an
immutable segment. Options:

| Approach | What breaks |
| --- | --- |
| Edit the segment in place | You just gave up immutability — every V2 dividend (lock-free reads, safe merging, mmap safety) dies with it |
| Rewrite the whole segment minus one doc | A 5 GiB rewrite to delete one tweet; deletes become O(segment) |
| …do nothing? | The doc keeps showing up in results — users notice immediately |

The move: **split "deleted" into two events on two timescales.**

1. **Logical delete — now.** Record the doc id in a side structure, the
   **tombstone overlay**. In this scaffold that's
   [`LiveDocs`](../src/merge.rs) — already implemented, just a
   `HashSet<DocId>` with `delete` / `is_live` / `deleted_count`. Search
   consults it per posting (the scorer's loop in
   [`Bm25::search`](../src/bm25.rs) skips any doc where `!live.is_live(id)`),
   so the doc vanishes from results **immediately**, before any merge runs.
   Cost: one hash insert. The segment file: untouched.
2. **Physical reclaim — later.** The doc's bytes still sit in the segment.
   They're dropped the next time a merge rewrites that segment — the merge
   simply *doesn't copy* tombstoned docs into its output. That's why
   [`merge`](../src/merge.rs) takes `live: &LiveDocs`, and why after a
   force-merge the shard's one segment reflects only live docs (a V4 Done-when
   box you can check with `ls -l`).

```
   t0  DELETE doc 17         → LiveDocs.delete(17)        1 hash insert
   t0+ε  search              → posting (17, tf 3) found in seg-2,
                               is_live(17) = false → SKIPPED. Gone from results.
   …hours pass; seg-2's bytes still contain doc 17's postings…
   t1  merge includes seg-2  → doc 17 not copied to output. Bytes reclaimed.
```

Cheap instant delete, deferred reclamation — you'll meet the exact pattern again
elsewhere: Lucene's per-segment `liveDocs` bitsets (this design, verbatim),
RocksDB's tombstone records dropped at compaction, and Postgres MVCC, where a
deleted row is a dead tuple until `VACUUM` — the merge — reclaims it. The shared
root cause is always the same: *the store never updates in place.*

Two fine points the tests will force you to face:

- **Nothing resurrected.** If a merge *ignored* tombstones, deleted docs would
  reappear in the merged segment — deletion would silently undo itself. That's
  the "nothing resurrected" clause in `prop_merge_preserves_live_docs`.
- **Tombstones are RAM-only here.** The scaffold's `LiveDocs` doesn't persist, so
  deletes die with the process (the SPEC lists persisting them as a stretch). A
  real engine writes them beside the segments. Know which contract you're
  shipping and state it in the design doc.

---

## 3. The merge itself: a k-way merge of sorted streams

What must be true of [`merge`](../src/merge.rs)'s output? V4's first Done-when
box says it exactly: **live postings of the output = union of the inputs' live
postings** — nothing lost, nothing resurrected, ordering preserved. And the last
box adds: search results are *identical* before and after. A merge changes
layout and speed, never answers.

The shape of the work (the scaffold's TODO names the stations; the design inside
them is yours):

- Each input's term dictionary is **sorted** (V2 guaranteed it). N sorted
  streams merge into one sorted stream without loading everything into RAM —
  the same k-way discipline as merge sort's merge step. (Loading it all and
  re-sorting works at toy scale and defeats the point; the SPEC says so.)
- Surviving docs get **renumbered** into the output's fresh contiguous id space
  — which means every posting's doc id gets remapped, and stored fields +
  lengths travel with their doc.
- The footer's `doc_count` / `total_length` are **recomputed from survivors** —
  get this wrong and you haven't corrupted the postings, you've corrupted
  *BM25's IDF and avgdl inputs* (V3 quietly mis-scores forever after).
- Output goes through the same [`SegmentWriter`](../src/segment.rs) as a
  refresh does — a merge is just a flush whose input is other segments.

---

## 4. The policy: merging is a budget, not hygiene

So merge constantly, keep S = 1 always? Look at what a merge *costs*: it
**rewrites live data**. Every merged byte is a byte written again. Merge after
every refresh and a document gets rewritten on every future refresh, forever.
This cost has a name — **write amplification**: total bytes written to disk per
byte of user data. A doc flushed once and then carried through 4 merge
generations has been written **5 times** (1 flush + 4 rewrites) — 5× write
amplification, real I/O bandwidth stolen from queries and real SSD wear.

So the two extremes are both wrong, and the policy is the dial between them:

| Policy | Write amp | Segment count | Verdict |
| --- | --- | --- | --- |
| Merge on every write | Maximal — constant rewriting | Always 1 | Burns I/O queries never get back |
| Never merge | 1 (write once) | Unbounded | Search degrades without bound; deletes never reclaim |
| **Tiered trigger** | Bounded | Bounded | Merge when a shard exceeds `merge_factor` segments — the scaffold's [`MergePolicy`](../src/merge.rs) |

The scaffold's `merge_factor` (from
[`EngineConfig`](../src/shard.rs)) is the tiered trigger's knob: merge only once
a shard holds more than that many segments. Within the trigger there's still a
choice [`MergePolicy::plan`](../src/merge.rs) leaves to you: merge *everything*,
or the smallest few? The one-line hint the scaffold gives is worth dwelling on —
**merging like-sized segments keeps write amplification down**, because merging
a tiny segment into a giant one rewrites the giant to absorb a crumb. (Follow
that thread far enough and you invent Lucene's tiered merge policy and LSM
leveling — the V4 stretch.) `force_merge` — collapse to exactly one segment — is
deliberately *manual* (`POST /_forcemerge`): maximum read speed, paid once, for
a shard that's done taking writes.

Whatever rule you pick, the SPEC's grading is the same as ever: **deliberate and
documented**, with the segment-count metric proving it holds under the boss
fight's continuous-indexing run.

---

## 5. The design space you must walk through (not around)

1. **The plan rule** — all segments past the trigger, or the smallest
   `merge_factor`? What does each do to write amplification and to worst-case
   segment count? Pick, measure, document.
2. **The k-way merge mechanics** — how do you walk N sorted dictionaries in
   lockstep without materializing them? What state does the frontier need?
3. **Doc-id remapping** — survivors renumber contiguously; where does the
   old→new map live and when can you drop it?
4. **The swap** — who owns the segment list, and what makes "replace inputs
   with output" atomic with respect to concurrent searches? (The `Arc` retiring
   discipline from V2 is doing load-bearing work here.)
5. **When merges run** — inline after refresh, or a background task? What does
   each mean for indexing latency?

`/hint` for a nudge on any of these; `/quest` for the guided build with
acceptance tests up front.

---

## 6. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Segment creep | Every refresh mints a segment; query cost grows with segment count even at constant data — write traffic degrades reads. |
| Merge | Rewrite N small immutable segments as 1, swap atomically, retire inputs; reads never block. |
| Tombstone | Logical delete now (a set membership check at query time), physical reclaim later (merge skips dead docs). |
| Nothing resurrected | A merge that ignores tombstones un-deletes documents — the property test's sharpest edge. |
| Write amplification | Every merge rewrites live bytes; a doc surviving 4 merges was written 5 times. |
| The policy | A budget between merge I/O and query cost — tiered trigger via `merge_factor`; extremes are both wrong. |
| Transparent | Results identical before/after a merge; only layout and speed change. |
| The gauge | Live segments per shard is the "am I merging enough" metric — bounded (≤ 20) under the boss fight. |
| Same shape elsewhere | Lucene liveDocs, RocksDB compaction, Postgres VACUUM — never-update-in-place always ends here. |

## 7. Where you'll build this

- **Module:** [src/merge.rs](../src/merge.rs) — the `todo!()`s in
  `MergePolicy::plan` and the free function `merge`
  ([`LiveDocs`](../src/merge.rs) is already implemented — study it, it's small).
- **Unlocks these V4 Done-when boxes** ([SPEC.md](../SPEC.md)):
  - [ ] Merged output = union of inputs' live postings; nothing lost or resurrected.
  - [ ] A deleted doc disappears from results immediately, pre-merge.
  - [ ] A merge physically drops tombstoned docs (force-merge → 1 segment, live-only size).
  - [ ] The merge policy is a deliberate, documented trigger.
  - [ ] Search results unchanged across a merge.
- **Proof artifacts:** `prop_merge_preserves_live_docs`, the delete-then-search
  test, the force-merge test, and the merge policy + delete model in
  `docs/20-design.md`.
