# The Sparse Index — Seek, Don't Scan

> What this teaches: how a consumer's "give me records from offset 4,000,000"
> becomes a couple of file seeks instead of a 400 MB scan — and why the index
> that makes it possible must be a disposable *hint*, never the truth. No prior
> knowledge of indexes or binary search assumed (we derive both).
>
> Prepares you for **V2** in [SPEC.md](../SPEC.md) ("Sparse offset index").
> Anchored to [src/index.rs](../src/index.rs) — the `maybe_index`, `lookup`,
> and `rebuild_from_log` `todo!()`s — and the read path in
> [src/log.rs](../src/log.rs)'s `read_from`. Read
> [00-the-append-only-log.md](00-the-append-only-log.md) first: this doc
> assumes its frames, offsets, and segments.

---

## 0. The one sentence to hold onto

**Remember where every ~4096th byte's record starts, binary-search those
breadcrumbs to land *near* your target, and walk the last few frames to hit
it exactly** — logarithmic seek time for a memory cost thousands of times
smaller than indexing everything.

---

## 1. The problem: offsets are logical, disk positions are bytes

V1 gave every record a logical **offset** — "the Nth record appended". A
consumer resumes from a committed offset (V4) by asking
`GET /topics/clicks/partitions/0/records?offset=4000000`.

But the log file is a byte array of *variable-length* frames. Offset 4,000,000
lives at… what byte? There's no arithmetic that answers this: record sizes
vary, so byte position is the *sum of all frame lengths before it*. Nothing
short of information about the file can convert one to the other.

The naive answer: start at byte 0, read frames, count. At an average of ~100
bytes per record, reaching offset 4,000,000 means scanning **~381 MiB** — for
*every fetch*. A consumer polling every second re-pays the scan every second.
This is the failure mode the SPEC's Done-when criteria outlaw: locating an
offset must be **sub-linear**, with the bytes scanned *independent* of how deep
the offset sits.

So we need a map from offsets to byte positions. The question is: *how many
entries?*

---

## 2. The design space: none vs dense vs sparse

Concrete numbers, for one 1 GiB segment of ~100-byte-average records
(≈ 10.7 million records), with 8-byte index entries (`u32` offset + `u32`
position — see [`IndexEntry`](../src/index.rs)):

| Strategy | Entries | Index size | Cost to find offset K |
| --- | --- | --- | --- |
| **No index** | 0 | 0 | scan from segment start — up to 1 GiB of reads |
| **Dense** (every record) | ~10.7 M | ~82 MiB | one lookup, exact — O(1) |
| **Sparse** (every ~4 KiB of log) | 262,144 | 2 MiB | binary search (18 steps) + scan ≤ 4 KiB |

- **No index** re-pays the scan on every fetch. Out.
- **Dense** looks perfect until you price it: ~8% of your *entire data volume*
  spent on the index, an index write on *every single append* (on the hot
  path you just made fast in V1), and at broker scale — billions of records —
  the index becomes its own storage problem to page, cache, and recover.
- **Sparse** pays 2 MiB per GiB (0.2%), can comfortably live *in memory*
  (`entries: Vec<IndexEntry>` in the scaffold), and bounds every lookup to a
  logarithmic search plus at most **one interval's worth** of forward
  scanning.

The sparse row is the trade Kafka ships (`.index` files, 4 KiB default
interval — the scaffold's `DEFAULT_INDEX_INTERVAL_BYTES` in
[main.rs](../src/main.rs) copies it). The key insight: **you don't need the
exact answer from the index — you need a *close* answer, because the log
itself (frames with `len` headers) can walk the final stretch.** The index
gets you to the right neighborhood; the frames get you to the door.

Why 18 steps? Binary search halves 262,144 entries per comparison, and
2¹⁸ = 262,144. Eighteen comparisons against an in-memory `Vec`, then one file
seek, then ≤ 4 KiB of frame-walking. Compare row one: up to a gigabyte.

---

## 3. The resolution path, traced end to end

The full journey of `fetch(offset = 7300)`, using the segment layout from doc
00:

```
data/topics/clicks/0/
├── 00000000000000000000.log        base 0
├── 00000000000000004096.log        base 4096   ◀── largest base ≤ 7300
└── 00000000000000009232.log        base 9232
```

**Step 1 — pick the segment (V1's filenames pay off).** The segment holding
offset 7300 is the one with the largest base offset ≤ 7300 → `…4096.log`.
Directory listing only; no file opened yet.

**Step 2 — go relative.** Within that segment, we want relative offset
7300 − 4096 = **3204**. This subtraction is why
[`IndexEntry.relative_offset`](../src/index.rs) is a `u32` and not a `u64`:
the base is already encoded in the *filename*, so entries don't repeat it —
each entry is 8 bytes instead of 16, doubling how much index fits in the same
memory. (And it's why byte `position` fits in a `u32` too: segments stay under
4 GiB by design.)

**Step 3 — binary-search the sparse entries** (`Index::lookup`,
[index.rs:99](../src/index.rs#L99)). Suppose the index holds (illustrative
numbers — entries land wherever ~4 KiB boundaries fell):

| `relative_offset` | `position` (byte) |
| --- | --- |
| 0 | 0 |
| 1021 | 98,304 |
| 2088 | 196,608 |
| **3149** | **294,912** ◀ largest entry ≤ 3204 |
| 4230 | 393,216 |

The answer is the *floor* entry: relative offset 3149, byte 294,912. Not our
record — but provably at-or-before it, and within one interval of it.

**Step 4 — seek and walk.** `seek` the `.log` to byte 294,912, then read
frames (each `len` header says where the next begins), counting up from 3149:
3149, 3150, … 3204 — found. Everything scanned in this step is ≤ one
`interval_bytes`, *no matter how deep in the segment 3204 is*. That bound is
literally a Done-when criterion, and the Proof asks you to instrument bytes
read to demonstrate it.

**Step 5 — the batch.** From there, `read_from` keeps decoding (CRC-checking
every frame, per V1) until `max_records` are collected or the segment/log
ends, and the route returns them with `next_offset` for the consumer to
continue from.

One boundary case is load-bearing enough to be its own Done-when box: a fetch
at an offset **at or past the log end** returns an *empty batch* — not an
error, not a hang. A tailing consumer (caught up, polling for new records)
lives at that boundary *permanently*; treating it as an error breaks every
consumer the moment it catches up. The scaffold's `read_from` doc comment
([log.rs:152-154](../src/log.rs#L152-L154)) pins this, and
[routes.rs](../src/routes.rs)'s fetch handler already leans on it
(`next_offset` stays put when the batch is empty).

---

## 4. How the index gets built — and stays sparse

On the append path (V1), after each frame is written, `Log::append` calls
`Index::maybe_index(relative_offset, position, frame_len)`
([index.rs:88](../src/index.rs#L88)). The "maybe" is the sparsity mechanism:
the index keeps a running `bytes_since_last` counter, and only when
~`interval_bytes` of log have accrued since the last entry does it record one
and reset the counter. Result: entry count ≈ `segment_bytes /
interval_bytes`, regardless of record count — the "far fewer entries than
records" Done-when box.

The interval is a genuine knob, and the SPEC wants it documented as one:

- **Halve it** (4 KiB → 2 KiB): worst-case scan halves; index memory doubles.
- **Double it**: memory halves; scans lengthen.

The right value depends on record sizes and fetch patterns — for a 1 GiB
segment, 4 KiB costs 2 MiB and bounds scans to ~40 average records; whether
that's your choice, and why, belongs in `docs/08-design.md`. (CONCEPTS.md's
depth probe asks exactly this.)

---

## 5. Hint, not truth: why the index must be rebuildable

Here is the principle this vertical exists to teach: **the log is the only
source of truth; the index is a cache of the log's geometry.**

Every fact in the index — "relative offset 3149 starts at byte 294,912" — is
*derivable* by scanning the log's frames from the start, tracking position and
count. The index adds zero information; it only pre-computes. Which means:

- **Deleting it loses nothing.** Reopen, scan the segment once,
  re-emit entries: `Index::rebuild_from_log`
  ([index.rs:109](../src/index.rs#L109)). The Done-when box demands exactly
  this: `rm` the `.index`, reopen, reads still resolve.
- **Corruption in it is an inconvenience, not an incident.** A suspect index
  is discarded and rebuilt. Compare: corruption in the *log* is data loss.
  Guess which file deserves the fsync discipline (V1) and which doesn't.
- **It never needs to be crash-consistent with the log.** If a crash catches
  the log one frame ahead of the index, the index is merely *slightly more
  sparse* than intended near the tail — lookups still work, because the index
  only ever promises "at or before". This is why `Index::open`'s TODO
  ([index.rs:64-66](../src/index.rs#L64-L66)) says a missing/short file just
  yields an empty index.

The trap named in [CONCEPTS.md](../CONCEPTS.md) is forgetting this status —
fsyncing index writes as if they were data (paying V1's durability tax on a
cache), or treating index corruption as fatal (turning a rebuildable hint into
a single point of failure).

This "derived structure, rebuildable from the log" shape recurs across the
repo: Raft state machines rebuilt from the Raft log (project 09), SSTable
block indexes (project 22), search-engine term dictionaries (project 20).
Learn it once here.

---

## 6. The design space — decisions the SPEC leaves to you

- **The `.index` file format.** The in-memory form is given
  (`Vec<IndexEntry>`, sorted); how entries are laid out on disk — and how
  `open` distinguishes "fine", "short", and "garbage" — is yours. Remember its
  status (§5) before you gold-plate it.
- **The floor search.** `lookup` needs "largest entry ≤ target" over a sorted
  `Vec`. Deriving that variant of binary search (and its empty-index and
  before-first-entry edges, where the answer is byte 0) is the small, real
  algorithmic exercise of V2 — don't rob yourself of it.
- **When to rebuild.** Eagerly on open when the file is missing? Lazily on
  first lookup? On CRC-mismatch? Any is defensible; write down which and why.
- **What "position" points at.** Frame start of the indexed record is the
  natural choice — but make sure append records the position *before* writing
  the frame it indexes, or your entries will be subtly off by one frame.

`/hint` for graduated nudges; `/quest V2` for the guided build with
acceptance tests up front.

---

## 7. Mental model summary

| Idea | One-line takeaway |
| --- | --- |
| Offset vs position | Offsets are logical (Nth record); bytes are physical; converting needs precomputed knowledge of the file. |
| Sparse beats dense | You only need a *close* answer — the log's own frames walk the last interval. 0.2% memory vs 8%. |
| Resolution path | filename → segment; subtract base → relative; binary-search floor → byte; seek + bounded scan → record. |
| Relative `u32` offsets | The base lives in the filename; entries don't repeat it — 8 bytes each, and the reason segments stay < 4 GiB. |
| Hint, not truth | The index is a cache of log geometry: rebuildable, allowed to be stale-at-the-tail, never fsync-precious. |
| Tail fetch = empty, not error | A caught-up consumer lives at the log end forever; "no records yet" is a normal answer. |

**Where you'll build this:** [src/index.rs](../src/index.rs) —
`maybe_index` ([line 88](../src/index.rs#L88)), `lookup`
([line 99](../src/index.rs#L99)), `rebuild_from_log`
([line 109](../src/index.rs#L109)), and the entry-loading TODO in `open`
([line 64](../src/index.rs#L64)) — plus wiring the seek into `Log::read_from`
([log.rs:166](../src/log.rs#L166)). It unlocks all five **V2 Done-when**
boxes: arbitrary-offset fetches, sub-linear location, documented sparsity,
delete-and-rebuild, and the clean tailing-consumer boundary.
