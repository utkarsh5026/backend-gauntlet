# The Inverted Index, Immutable Segments & mmap — From First Principles

> A ground-up guide to the data structure a search engine *is* — the inverted
> index — and the two on-disk ideas that make it scale: immutable segments and
> memory-mapped reads. No prior knowledge of Lucene, mmap, or the page cache
> assumed. This prepares you for **V2** in [SPEC.md](../SPEC.md): the `todo!()`s
> in [`SegmentWriter::add`/`flush`](../src/segment.rs) and
> [`SegmentReader::open`/`postings`](../src/segment.rs) are what you're about to
> build. Anchored to [segment.rs](../src/segment.rs), [doc.rs](../src/doc.rs),
> and [index.rs](../src/index.rs).

---

## 0. The one sentence to hold onto

**Instead of asking each document "do you contain `rust`?", store the answer
backwards — for each term, the list of documents containing it — so a query is a
dictionary lookup plus a list walk: O(hits), not O(corpus).**

Everything else in V2 — segments, refresh, immutability, mmap — exists to make
that structure survive contact with disk, writes, and concurrency.

---

## 1. The problem: the forward index answers the wrong question

The natural way to store documents is forward: `doc → its terms`.

```
doc 0 → [rust, fast, safe]
doc 1 → [go, fast]
doc 2 → [rust, tutorial]
```

Query "which docs contain `rust`?" against this and you must open **every
document** — a full scan, every query, forever:

| Forward index (scan) | What breaks |
| --- | --- |
| Check each of N docs per query | O(corpus) per query. Latency grows linearly with data, independent of how many docs actually match. |
| 1M docs × 1 µs/doc | ≥ 1 second per query before you've ranked anything. The SPEC's boss fight wants **p99 ≤ 50 ms at 2,000 searches/sec**. |
| Cache can't save you | The long-tail query mix (Zipfian) means most queries are seen once — they all pay the scan. |

**Invert it.** Map each term to the (sorted) list of documents containing it —
each entry a **posting** carrying the doc id and how often the term occurred
(that's [`Posting`](../src/doc.rs), fed by V1's `AnalyzedDoc.term_freqs`):

```
                 term dictionary          postings lists
                ┌──────────────┐
                │ fast     ────┼──►  (doc 0, tf 1) (doc 1, tf 1)
                │ go       ────┼──►  (doc 1, tf 1)
                │ rust     ────┼──►  (doc 0, tf 1) (doc 2, tf 1)
                │ safe     ────┼──►  (doc 0, tf 1)
                │ tutorial ────┼──►  (doc 2, tf 1)
                └──────────────┘
                 sorted, so a          sorted by doc id, so lists
                 lookup is a           can be walked / intersected
                 binary search         in one pass
```

Now "which docs contain `rust`?" is: find `rust` in the dictionary (binary search
— the dictionary is sorted), walk its postings list. Cost tracks the number of
**hits**, not the size of the **corpus**. A term matching 40 documents costs ~40
steps whether the corpus holds ten thousand documents or ten billion. This is the
complexity flip the whole project is built on.

The flip has a price, and it's the reason the rest of V2 exists: **updates are
now awful.** Adding one document touches *every term it contains* — dozens of
postings lists, each of which must stay sorted. Updating a structure like this in
place, on disk, while queries read it concurrently, is a nightmare of locks and
torn reads. So we don't.

---

## 2. Immutable segments: never edit, only add

The move (Lucene's move, verbatim): **never modify an index file. Ever.**

- New documents accumulate in an **in-memory buffer** (inside
  [`Index`](../src/index.rs), the per-shard owner).
- A **refresh** flushes the buffer into a brand-new **segment** — a
  self-contained mini inverted index in one file — via
  [`SegmentWriter::flush`](../src/segment.rs). The file is complete at birth
  and never touched again.
- A shard is an **ordered pile of segments**. A query consults all of them and
  unions the results.

```
   time ──────────────────────────────────────────────►

   index 3 docs        refresh        index 2 docs      refresh
        │                 │                │                │
        ▼                 ▼                ▼                ▼
   [ buffer: 3 ]  ──►  seg-0 (3 docs)  [ buffer: 2 ] ──► seg-1 (2 docs)
                       immutable                          immutable

   a search after the 2nd refresh consults: seg-0 ∪ seg-1
```

### Dividend 1: lock-free concurrent reads

A reader can't race a writer over data that **never changes**. Fifty concurrent
searches read the same segment with zero synchronization — no `RwLock` on the
read path, no torn state — because there is nothing to synchronize *with*. The
scaffold encodes this as `Arc<SegmentReader>`: shared freely, retired only when
the last search drops its handle. Contention is confined to the write path (the
in-memory buffer), which is exactly the "deliberate contention model" the
horizontal checklist asks you to state.

### Dividend 2: safe background merging

Because segments never change, a merge (V4) can read them at leisure, write a
combined replacement beside them, and atomically swap — all while searches keep
reading the old files. In-place mutation makes that a locking problem;
immutability makes it a bookkeeping problem.

### The cost: the near-real-time (NRT) contract

A just-indexed document sits in the buffer, and **the buffer is not searched**.
It becomes visible only when a refresh mints a segment. This is a *feature you
must document*, not a bug: the refresh interval is the knob trading searchability
latency (refresh often → docs visible fast) against segment churn (refresh often
→ hundreds of tiny segments → V4's problem). Elasticsearch defaults to refreshing
every 1 s and calls itself "near-real-time" for exactly this reason.

Resist the tempting "fix" of letting search peek into the buffer so documents are
instantly visible — that quietly puts a **mutable** structure back on the
lock-free read path and undoes dividend 1. The NRT contract exists to keep
mutation and search decoupled. (This is V2's second Done-when box: a doc is
findable only after refresh, *and that's written down*.)

---

## 3. On disk: the format is the challenge

A segment must be one file that a reader can navigate **without loading it into
memory**. The scaffold's doc comment sketches a workable layout — the details are
your design, but the *jobs* each region must do are fixed:

```
  ┌───────────────────────────────────────────────────────────────┐
  │ stored docs      │ postings blocks │ term dictionary │ footer │
  │ (id, text, len)  │ one block per   │ SORTED terms →  │ where  │
  │ so hits render   │ term            │ (offset, df)    │ is the │
  │ without a 2nd    │                 │                 │ dict?  │
  │ store            │                 │                 │ counts │
  └───────────────────────────────────────────────────────────────┘
        ▲                    ▲                  ▲            ▲
        │                    │                  │            │
   stored(doc_id)      postings(term)     binary-search   open() reads
   doc_length(doc_id)  decodes one        *in the mapped  this FIRST to
                       block in place     bytes*           find the rest
```

Three properties are non-negotiable, because the reader's methods
([`postings`](../src/segment.rs), [`doc_length`](../src/segment.rs),
[`stored`](../src/segment.rs)) depend on them:

1. **The term dictionary is sorted**, so a lookup is a binary search over the
   mapped bytes — no hash table to deserialize, no heap structure to build on
   open.
2. **Postings decode in place** — a block's bytes are enough to reconstruct its
   `Vec<Posting>` without reading anything else.
3. **The footer is the map**: it's written last (so it exists only if everything
   before it does) and read first, carrying the dictionary offset plus
   `doc_count` and `total_length` — which are not trivia; they're BM25's IDF and
   `avgdl` inputs (V3), which is why [`SegmentReader`](../src/segment.rs)
   surfaces them as fields.

And because the reader trusts these bytes, **corruption must be detected, not
obeyed** — a truncated file or a flipped byte should surface as
`AppError::CorruptSegment` on open/read, never as silently wrong postings. Magic
bytes, length checks, a checksum: your choice, but *something* stands between
`mmap` and garbage. (Also why `flush` fsyncs before returning — a crash must not
leave a half-written file that a future `open` will happily map.)

### Why postings compress absurdly well (the stretch)

Postings store doc ids sorted ascending. Sorted means you can store **gaps**
instead of absolute ids, and gaps are small numbers, and small numbers fit in
few bytes (varint: 7 bits of payload per byte). Verified example:

```
doc ids:      [4, 11, 15, 100, 103]      raw u32s:      20 bytes
gaps:         [4,  7,  4,  85,   3]      varint gaps:    5 bytes   (4×)
```

The denser the term, the smaller the gaps: a term appearing in 100k of 1M docs
has average gap ~10 — one byte each, ~100 KB instead of 800 KB of raw u64s. This
is why the SPEC lists delta+varint as V2's stretch: it's optional here, but it's
the reason real engines can keep hundred-million-doc postings hot in RAM.

---

## 4. mmap: read the file without reading the file

The last piece: how does [`SegmentReader`](../src/segment.rs) access a file that
might be 10 GiB? The obvious way and its cost:

| `read()` the file into a `Vec<u8>` | What breaks |
| --- | --- |
| The whole segment is copied into your heap | 10 GiB segment → 10 GiB of process memory, before the first query |
| Every process re-copies it | Kernel has the data in its page cache *and* you have a private copy — paid twice |
| Startup cost | Opening a shard means reading every segment front to back |

`mmap` is the alternative: ask the kernel to map the file into your **address
space**. You get back what Rust sees as a `&[u8]` covering the whole file — but
**no bytes are read yet**. The mapping is a promise, not a copy:

```
   your virtual memory                        disk
  ┌────────────────────┐
  │  mmap: &[u8] view  │   page fault    ┌──────────────┐
  │  [............]────┼───on first──────│ segment file │
  │        ▲           │   touch (4 KiB  └──────────────┘
  │        │           │   at a time)           ▲
  └────────┼───────────┘                        │
           │              ┌─────────────────────┘
           │              ▼
           │        OS page cache  ← shared, kernel-managed RAM
           └─────── hot pages stay resident; cold pages get
                    evicted under memory pressure — for free
```

- **Touch a byte** → the kernel faults in that 4 KiB page from disk.
- **Touch it again** → it's in the page cache; RAM speed, no syscall.
- **Memory pressure** → the kernel evicts cold pages. Your heap never grew.

So a query for a hot term costs a few binary-search probes into
already-resident dictionary pages plus one postings block — a few KiB resident
against a 10 GiB file. That's V2's Done-when box "resident set stays far below
segment size for a cold, large segment": it's directly observable, and it's the
page cache doing it, not your code.

What you *give up* versus a hand-rolled cache: control. You can't pin a term,
can't set an eviction policy, can't account memory per-query — the kernel decides.
(Project 22 builds the hand-rolled block cache; building both is the point of the
pair.) One Rust-flavored caveat since you'll hold `Mmap` (from `memmap2`): the
kernel offers no guarantee about the file changing under a live map — which is a
non-problem here *precisely because* segments are immutable. The two ideas of V2
lock together: immutability is what makes mmap safe.

---

## 5. The design space you must walk through (not around)

The `todo!()`s leave you real decisions; the SPEC grades that you made them
deliberately, not which you picked:

1. **The exact byte layout** — fixed-width or length-prefixed records? How does
   the binary search know where each dictionary entry starts? (This is *the*
   design problem of V2. Sketch it on paper before writing `flush`.)
2. **Postings encoding** — plain `[len][(doc_id, tf)…]` to start, or
   delta+varint now? (SPEC's advice: plain is fine to start.)
3. **Corruption detection depth** — magic + footer sanity, or a real checksum?
   What does the corruption *test* need to be able to trigger?
4. **What exactly is fsynced, when** — the file, the directory entry, both?

When you've sketched the layout, `/hint` can nudge a stuck spot and `/quest`
runs the guided build with acceptance tests written first.

---

## 6. Mental-model summary

| Idea | One-line version |
| --- | --- |
| Inverted index | `term → sorted postings`; query cost tracks hits, not corpus. |
| The flip's price | Cheap lookups, expensive updates — which forces the segment design. |
| Segment | An immutable, self-contained mini-index file, minted by refresh, never edited. |
| NRT contract | Buffered docs are invisible until refresh; the interval is a documented knob. |
| Immutability's dividends | Lock-free concurrent reads; merges can rewrite safely alongside them. |
| On-disk format's jobs | Sorted dictionary (binary-searchable in place), self-decoding postings, footer as map, corruption detected on open. |
| mmap | A `&[u8]` *view* backed by the page cache — address space, not heap; the kernel caches and evicts for free. |
| Immutability ⇄ mmap | Mapping a file that can change is hazardous; segments can't change — the ideas need each other. |

## 7. Where you'll build this

- **Module:** [src/segment.rs](../src/segment.rs) — the `todo!()`s in
  `SegmentWriter::{is_empty, add, flush}` and
  `SegmentReader::{open, postings, doc_length, stored}`.
- **Unlocks these V2 Done-when boxes** ([SPEC.md](../SPEC.md)):
  - [ ] Flush → reopen → `postings(term)` returns exactly the right docs, sorted, with right tfs.
  - [ ] A doc is searchable only after refresh (NRT contract, documented).
  - [ ] Segments are immutable — new files, never rewrites.
  - [ ] Queries answer from the mmap; resident set ≪ segment size.
  - [ ] Truncated/byte-flipped segments surface as errors, never wrong postings.
- **Proof artifacts:** round-trip tests, the refresh-visibility test, a corruption
  test, the flat postings-lookup-latency bench, and the on-disk format written
  into `docs/20-design.md`.
