# The Segmented Append-Only Log — From First Principles

> What this teaches: why every serious storage system (Kafka, Postgres's WAL,
> Raft) is built on a file you only ever *append* to — and the two lies hiding
> inside "I wrote it to disk". No prior knowledge of disks, `fsync`, or Kafka
> assumed.
>
> Prepares you for **V1** in [SPEC.md](../SPEC.md) ("Segmented append-only
> log"). Anchored to [src/log.rs](../src/log.rs) — the `Log::append` and
> `Log::read_from` `todo!()`s and the recovery TODO in `Log::open` are what
> you'll build after reading this — plus the record types in
> [src/record.rs](../src/record.rs).

---

## 0. The one sentence to hold onto

**A commit log is a file you refuse to overwrite** — records only ever go on
the end, in fixed-size segment files named by the offset they start at — and
that one refusal is what makes it simultaneously the *fastest* thing a disk can
do, *recoverable* after a crash, and *cheap* to expire.

Everything in V1 — offsets, framing, CRCs, segment rolling, torn-tail recovery,
the fsync dial — falls out of that sentence. This doc derives each piece.

---

## 1. The problem: absorb a firehose, lose nothing, delete cheaply

A broker's storage layer has three jobs that fight each other:

1. **Absorb writes at disk speed.** Producers may hammer it with hundreds of
   MB/s of records.
2. **Never lose a committed record.** Once the broker says "stored at offset
   42", a power cut one millisecond later must not un-store it.
3. **Delete old data cheaply.** A broker retains a *window* of history; last
   month's records must be droppable without touching this month's.

The obvious designs each lose at least one:

| Naive design | What breaks |
| --- | --- |
| A database table (`INSERT` per record) | Every insert updates the table *and* its indexes *and* the WAL — you pay for random-access machinery (B-trees, MVCC) you never use, because you only ever read "everything after position X". Throughput dies first. |
| One file per record | A million records = a million files. Directory operations, inode exhaustion, and reading a range means a million `open()` calls. |
| One big file, records updated/deleted in place | Deleting old data means rewriting the file (O(all data)). Worse: an in-place overwrite interrupted by a crash leaves the file in a state that's *neither* the old nor the new version. |
| One big append-only file, forever | Better! But now deleting old data still means rewriting, and the file grows without bound. (We'll fix this with *segments* in §6.) |

The design that wins all three jobs: **append-only, segmented**. Records are
written only at the tail (job 1: sequential writes, see §2), completed records
are never touched again (job 2: nothing to corrupt but the tail, see §5), and
history is split into segment files so expiring old data is `rm old-file`
(job 3, see §6).

This is what [src/log.rs](../src/log.rs) scaffolds: a `Log` is a directory of
`Segment` files; `append` writes a framed record at the tail and hands back an
offset; `read_from` reads without ever mutating.

---

## 2. Why appending is the fastest thing a disk does

This isn't folklore; it's mechanical.

**Spinning disks.** A hard drive is a physical arm over a spinning platter.
Reading or writing byte X means (a) moving the arm to X's track (a *seek*,
several milliseconds) and (b) waiting for the platter to rotate X under the
head. Random writes pay (a)+(b) *per write* — a few hundred operations per
second, no matter how small each write is. Sequential writes pay it *once*,
then stream at the platter's raw transfer rate — commonly 100–200 MB/s. The
gap between "random small writes" and "sequential streaming" is several orders
of magnitude, and it is entirely the arm's fault.

**SSDs — where people assume the gap disappears. It doesn't.** An SSD has no
arm, but flash has a rule: you can write a *page* (~4–16 KiB) only into erased
space, and you can only erase a whole *erase block* (hundreds of KiB to MiB) at
a time. Overwriting one 4 KiB page in the middle of a block means the drive
must eventually copy the block's still-live pages elsewhere and erase the
block — extra internal writes for your one write, called **write
amplification**. Random small overwrites maximize it (and burn flash lifespan);
sequential appends fill blocks front-to-back so whole blocks die together and
nothing needs copying. Sequential still wins — for a different mechanical
reason.

**And one layer up:** appends also let the OS and the drive *batch*. A hundred
appends become one large contiguous write; a hundred random writes can't be
merged into anything.

That is the whole performance story of Kafka in one idea, and it's why the
SPEC's boss-tier throughput numbers are achievable from a laptop: you're asking
the disk to do the one thing it's genuinely great at.

---

## 3. The frame: how records live inside a file of bytes

A file is just a byte array. If you append records naively —
`file.write(record_bytes)` — you can never read them back, because nothing
says where one record ends and the next begins. So every record is wrapped in
a **frame** with a small header. The scaffold
([log.rs's module doc](../src/log.rs)) names this layout:

```
[len: u32][crc: u32][timestamp: i64][key_len: u32][key bytes][value bytes]
 ~~~~~~~~  ~~~~~~~~  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 4 bytes    4 bytes                the "body"
```

The header is 4 + 4 + 8 + 4 = **20 bytes** before the key and value. Trace one
concrete record through it — key `user-42` (7 bytes), value
`{"event":"click"}` (17 bytes):

| Field | Bytes | Content |
| --- | --- | --- |
| `len` | 4 | frame/body length — *you* decide exactly what it counts (see §7) |
| `crc` | 4 | checksum of the bytes it protects (see below) |
| `timestamp` | 8 | e.g. `1752451200000` (epoch millis) |
| `key_len` | 4 | `7` |
| `key` | 7 | `user-42` |
| `value` | 17 | `{"event":"click"}` |
| **total** | **44** | one frame on disk |

Each field earns its place:

- **`len` makes the file self-describing.** A reader at a frame boundary reads
  4 bytes, learns how far the frame extends, and can jump to the next
  boundary. Without it, record boundaries are unrecoverable.
- **`crc` makes corruption *detectable*.** CRC-32 is a 4-byte fingerprint of
  the bytes it covers. As a worked example: the 36-byte body above (big-endian
  encoding) has CRC-32 `0x556b171c`; flip a *single bit* anywhere in it and the
  CRC becomes `0xcab19482` — completely different. (Your exact values will
  differ with your layout/endianness; the point is that any change is loud.)
  On read, recompute the CRC of what's on disk and compare to the stored one.
  Mismatch → [`AppError::CorruptFrame`](../src/error.rs), *never* returned as
  data. That's a V1 Done-when box.
- **`key_len` splits the body** into key and value (the key is optional —
  `key_len = 0` can mean "no key"; that encoding choice is yours).

And the **offset**? Notice it's *not in the frame*. An offset is a *logical*
position — "the Nth record ever appended" (`type Offset = u64` in
[record.rs](../src/record.rs)) — not a byte position. The log assigns it at
append time from `next_offset` and returns it; record N's offset is always N.
That's the "monotonically increasing, equals the count before it" Done-when
criterion, and it's what makes offsets meaningful cursors for consumers (V4)
and resolvable positions for the index (V2).

---

## 4. The first lie: `write()` returning does not mean "on disk"

Here is the full journey of your 44-byte frame:

```
your buffer ──write()──▶ kernel page cache ──(later, OS decides)──▶ disk platter
                              │
                              └── fsync() forces THIS arrow, now,
                                  and blocks until it completes
```

When `write()` returns, your bytes have moved into the **page cache** — kernel
RAM. The OS flushes that to the physical disk *eventually*, on its own
schedule (often tens of seconds later). Between those two moments, a power cut
loses the data — even though every call you made "succeeded".

Three operations, three different arrows:

| Call | What it actually moves |
| --- | --- |
| `write()` | your buffer → page cache. **Not durable.** |
| `flush()` (`BufWriter` etc.) | *your process's* userspace buffer → page cache. Still not durable — it never leaves RAM. |
| `fsync()` (`File::sync_all` in Rust) | page cache → the platter, **blocking until the hardware confirms**. This is the only durability arrow. |

(Fine print worth knowing: creating a *new file* also requires fsyncing the
*directory*, because the file's directory entry is itself data that lives in
the page cache. This bites exactly once — on segment roll.)

So durability has a price: an fsync is a full round-trip to the hardware —
typically *orders of magnitude* slower than a buffered `write()`. That gives
you a genuine dial, and the SPEC demands you set it **deliberately**:

| Policy | Throughput | On power loss you lose |
| --- | --- | --- |
| fsync every append | slowest — every record pays the hardware round-trip | nothing acknowledged |
| fsync every N records / every T ms | fast — one fsync amortizes over many appends | at most the un-fsynced window (bounded, known) |
| never fsync (trust the OS) | fastest | up to ~30 s of "acknowledged" records — you lied to every producer in that window |

The middle row is how real systems square the circle, and it hides a lovely
idea called **group commit**: while one fsync is in flight, queue the appends
that arrive behind it and let the *next* fsync cover all of them at once. Fifty
producers, one fsync — each waits at most one fsync's latency, and throughput
scales with batching. (CONCEPTS.md's depth probe; worth sketching in your
design doc.)

Which policy is right? That depends on what your *ack* means — if the produce
response promises durability, the fsync must happen before the response. There
is no universal answer; there is only a documented one. That's the Done-when
criterion: the policy is a *choice you wrote down* in `docs/08-design.md`, with
a `bench/` number showing what it costs.

---

## 5. The second lie: a crash leaves a *torn tail*, not a clean file

Say the process (or the whole machine) dies halfway through appending our
44-byte frame. The file now ends with, say, 19 of the 44 bytes:

```
  ...previous, complete frames... │ [len=44][crc=0x556b...][timesta─ ✂ CRASH
                                  │ ◀──────── 19 bytes of a 44-byte frame
                            last clean
                            boundary
```

A naive reader walking the file hits this tail, reads `len = 44`, tries to
read 40 more body bytes, and gets garbage or EOF mid-frame. If your code
shrugs and returns what it got, you have served a consumer *bytes that were
never a record*. This is the failure the SPEC calls out by name.

The framing from §3 is what turns this from a disaster into a detectable
condition. On reopen (`Log::open`'s `TODO(V1 recovery)` at
[log.rs:118](../src/log.rs#L118-L121)), recovery walks frames in the *active*
(last) segment; a tail frame whose length runs past EOF or whose CRC doesn't
match is provably incomplete. The contract the Done-when box demands:

- **truncate** the file back to the last clean frame boundary,
- restore `next_offset` from what survives,
- lose **at most the one in-flight write** — never a completed one (it was
  fsynced per your §4 policy), and never serve the garbage.

Why is CRC needed *in addition to* fsync? Because fsync only orders writes
against crashes — it doesn't detect a disk sector that partially wrote, or a
bit that rotted six months later. The CRC catches what fsync can't.

One trap from [CONCEPTS.md](../CONCEPTS.md), worth engraving: **a green happy-path
test suite cannot distinguish a durable log from a lucky one.** Tests rarely
crash between `write` and `fsync`. That's why the Proof line demands a
*torn-tail test* — deliberately write a partial frame, reopen, assert clean
truncation — and a *corruption test* — flip a byte, assert `CorruptFrame`.

---

## 6. Segments: why the log is many files, not one

Two hard reasons the log can't be a single ever-growing file:

**Retention.** The SPEC (and real brokers) expire old data. Deleting the first
half of a single file means rewriting the file — O(all live data), while
producers are still appending to it. Split the log into fixed-size **segment**
files instead, and retention becomes: delete the oldest *whole files*. O(1)
per segment, no rewriting, no coordination with the writer (which only ever
touches the *last* segment). This is exactly how Kafka expires data, and it's
the "retention deletes whole segments" horizontal item.

**Bounded offsets inside the index.** V2's sparse index stores byte positions
as `u32` — so a segment must stay under 4 GiB (`u32::MAX` = 4,294,967,295 ≈
4 GiB). Capping segment size keeps positions small and per-segment recovery
scans bounded. ([main.rs](../src/main.rs) defaults `SEGMENT_BYTES` to 64 MiB —
deliberately small so your tests actually roll segments; Kafka's default is
1 GiB.)

The naming scheme does real work. Each segment file is named by the **base
offset** of its first record, zero-padded to 20 digits (`Segment::create` at
[log.rs:57](../src/log.rs#L57) uses `format!("{base_offset:020}")`):

```
data/topics/clicks/0/
├── 00000000000000000000.log     ← offsets 0 ..= 4095 live here
├── 00000000000000000000.index
├── 00000000000000004096.log     ← offsets 4096 ..= 9231
├── 00000000000000004096.index
├── 00000000000000009232.log     ← the ACTIVE segment: appends go here
└── 00000000000000009232.index
```

Now "which segment holds offset 7300?" is answered by a **directory listing**:
sort the filenames (zero-padding makes lexicographic order = numeric order),
pick the largest base offset ≤ 7300 → `...4096.log`. No file is opened, no
data is scanned. `base_offset_of` at [log.rs:172](../src/log.rs#L172-L174)
parses the name back; that lookup is a V1 Done-when box, and V2 picks up from
there *inside* the segment.

**Rolling** is the append-path rule: when the active segment exceeds
`LogConfig::segment_bytes`, create a new `Segment` whose base offset is the
current `next_offset` and append there instead. Old segments are **sealed** —
never written again — which is precisely what makes them safe to index, cheap
to delete, and trivially correct under concurrent reads.

---

## 7. The design space — decisions the SPEC leaves to you

This doc stops here, because the rest is the interesting part. The Done-when
criteria pin the *behavior*; these choices are how you get there, and
`docs/08-design.md` is where you defend them:

- **Exact frame semantics.** What does `len` count — the body only, or
  header included? What does the CRC cover — and is `len` inside or outside
  its protection? (Think about which corruptions each choice can detect,
  and what recovery can trust while scanning a suspect tail.)
- **The fsync policy** (§4): per-append, every-N, every-T, or grouped — and
  what your produce ack means as a result. This is graded and benchmarked.
- **Recovery's scan** (§5): what exactly do you validate per frame, and when
  do you decide "this is the truncation point" vs "this is mid-log corruption"
  (which is *not* a torn tail and deserves a different response)?
- **The write handle.** The scaffold's `Segment` notes
  ([log.rs:45](../src/log.rs#L45-L46)) you'll want a persistent append handle
  and current write position rather than reopen-and-seek per append — how you
  hold that state is yours.

When you're ready to build, `/hint` gives graduated nudges and `/quest V1`
runs the guided build with acceptance tests written up front.

---

## 8. Mental model summary

| Idea | One-line takeaway |
| --- | --- |
| Append-only | Never overwrite → sequential speed, crash-safety, concurrent-read safety, all at once. |
| Offset | Logical position (Nth record appended), assigned by the log — not a byte address, not stored in the frame. |
| Frame (`len` + `crc`) | `len` makes bytes self-describing; `crc` makes corruption loud instead of silent. |
| `write()` vs `fsync()` | `write` moves bytes to kernel RAM; only `fsync` moves them to the platter. Durability is the fsync dial, set on purpose. |
| Torn tail | A crash mid-append leaves a partial frame; recovery detects it (len/CRC) and truncates to the last clean boundary. |
| Segments | Fixed-size files named by base offset → retention is `rm`, offset→segment lookup is a directory listing, sealed segments are immutable. |

**Where you'll build this:** [src/log.rs](../src/log.rs) —
`Log::append` ([line 149](../src/log.rs#L149)), `Log::read_from`
([line 166](../src/log.rs#L166)), and the recovery TODO in `Log::open`
([lines 118–121](../src/log.rs#L118-L121)). It unlocks all seven **V1
Done-when** boxes in [SPEC.md](../SPEC.md): monotonic offsets, restart
survival, byte-identical reads + corruption detection, segment rolling,
base-offset filenames, torn-tail truncation, and a documented fsync policy.

**In the wild:** Kafka's log (this is a faithful miniature), Postgres/MySQL
WALs, Raft log storage (project 09), event-sourcing stores (project 21) — and
the torn-tail + fsync discipline returns, hardened, in project 22's WAL.
