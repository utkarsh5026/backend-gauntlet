# Reading the Container: ISO-BMFF & Sample Tables — From First Principles

> A beginner-friendly guide. **No prior knowledge of video files assumed.**
> This teaches the *idea* behind **V1** so you can write the demuxer yourself.
> It prepares you for [`src/isobmff.rs`](../src/isobmff.rs) — the `demux()` and
> `read_box_header()` `todo!()`s — and the "Done when ALL true" list in
> [`SPEC.md`](../SPEC.md) for V1. It does **not** contain the parser; the interesting
> part is yours to build.

---

## The one sentence to hold onto

**An `.mp4` file is not "the video" — it is a little filesystem whose index
describes, for every single frame, where its bytes live, when it decodes, when it
displays, and whether it can stand alone; V1 is turning that cross-referenced index
into one flat list.**

---

## 1. Start concrete: what is even *in* an mp4?

Open a `.mp4` in a hex editor and you will *not* see a stream of pixels. You'll see
this near the front:

```
00 00 00 20 66 74 79 70 69 73 6f 6d ...
└──size u32──┘└─ "ftyp" ─┘└ "isom" ...
   = 32 bytes
```

Those four ASCII bytes `66 74 79 70` spell **`ftyp`** ("file type"). The four bytes
before them are a **length**. That pattern — *length, then a 4-letter name, then that
many bytes of payload* — repeats for the entire file. Each such unit is a **box**
(the spec's word; older docs say "atom"). The format is **ISO Base Media File
Format**, ISO-BMFF, and MP4/MOV/CMAF/HEIF are all dialects of it.

So the very first thing to internalize:

> An mp4 is a **tree of length-prefixed boxes**. Nothing more mysterious than that.

Here are the real fourcc byte values you'll be matching on (verified, big-endian):

| Box | ASCII bytes (hex) | What it holds |
|-----|-------------------|---------------|
| `ftyp` | `66 74 79 70` | brand / compatibility |
| `moov` | `6d 6f 6f 76` | **the index** — all metadata, no media |
| `trak` | `74 72 61 6b` | one track (video, or audio, …) |
| `mdia` `minf` `stbl` | … | nesting down to the sample tables |
| `mdat` | `6d 64 61 74` | **the media** — the actual coded frame bytes |

Two boxes carry everything that matters: **`moov`** (the index) and **`mdat`** (the
media). The whole tree exists to let a parser find `moov` and decode it.

---

## 2. Why length-prefixing is the whole trick

Why length *before* payload, instead of a magic marker at the end? Because it makes
the format **forward-compatible by construction**:

```
[ size=32 | "ftyp" | 24 bytes we understand ]
[ size=90814 | "moov" | ...the index... ]
[ size=17 | "uuid" | 9 bytes we've NEVER seen ]   ← skip: pos += 17
[ size=... | "mdat" | ...media... ]
```

A parser that hits a box type it doesn't recognize doesn't crash — it reads the
length and **jumps** `pos += size` to the next sibling. New box types can be added to
the format forever; old parsers step over them cleanly. This is the same idea as a
TLV (type-length-value) wire format.

There are two escapes in the header you must handle:

- `size == 1` → the real size is a **64-bit** value in the 8 bytes *after* the type
  (used when a box, usually `mdat`, is bigger than 4 GB).
- `size == 0` → "this box runs to the end of the file" (only the last box).

```
Normal:  [ size:u32 ][ type:u32 ][ payload ... ]          header = 8 bytes
Large:   [ 1:u32    ][ type:u32 ][ size:u64 ][ payload ]  header = 16 bytes
```

`read_box_header()` in the scaffold is exactly this. Its `todo!()` note says *"parse
a box header with 32/64-bit size + bounds checks"* — the bounds checks are the point
(see §7).

---

## 3. The tree you have to walk

`moov` is not flat — it nests. To reach the tables you dive:

```
moov
 └── trak                         (one per track)
      └── mdia
           ├── mdhd               → timescale  (ticks per second)
           ├── hdlr               → kind: video? audio?
           └── minf
                └── stbl          ← THE SAMPLE TABLES live here
                     ├── stsd     codec setup (avc1 → avcC = SPS/PPS)
                     ├── stts     decode durations
                     ├── ctts     composition offsets (B-frames)
                     ├── stsc     sample → chunk mapping
                     ├── stsz     sample sizes
                     ├── stco / co64   chunk file offsets
                     └── stss     which samples are keyframes
```

The scaffold's [`demux()` TODO](../src/isobmff.rs) is literally this descent:
*find `moov`, for each `trak` descend `mdia/minf/stbl`, read the tables, pull
`timescale` from `mdhd` and kind from `hdlr`.* Your output type is already defined —
a [`Track`](../src/isobmff.rs) with a `Vec<Sample>` where each `Sample` is
`{ offset, size, decode_time, duration, composition_offset, is_sync }`.

---

## 4. Why the media is described by *separate* tables (not inline)

Here's the naive design a beginner would reach for, and why the format rejects it.

**Naive:** store each frame as `[timestamp][keyframe?][length][bytes]`, one after
another. Self-describing! But:

| Problem with inline | Consequence |
|---------------------|-------------|
| Timing interleaved with bytes | Can't build a seek index without scanning the whole file |
| Every frame repeats structure | Wastes space; timing doesn't compress |
| Can't rewrite media without rewriting timing | Editing is O(file) |

ISO-BMFF's answer: **separate the geometry (where/how big) from the timing (when),
and compress each independently.** The `stbl` tables are that separation. Each table
encodes *one dimension* of the per-sample list, and cleverly:

- **`stts` — decode durations, run-length encoded.** Most frames have identical
  duration. So instead of `[40,40,40,40,…]` a million times, it stores
  *(count=1000000, delta=40)*. One entry. Timing is *extremely* compressible this
  way — that's *why* the format splits it out.
- **`stsz` — sizes.** One size per sample (or a single "all samples are N bytes").
- **`stsc` + `stco`/`co64` — chunk geometry.** Samples are grouped into **chunks**;
  `stco` gives each chunk's absolute file offset, `stsc` says "chunks 1–5 hold 30
  samples each, chunks 6+ hold 24 each" (also run-length). A sample's offset is
  `chunk_offset + sum(sizes of earlier samples in that chunk)`.
- **`stss` — the keyframe list.** A sparse list of sample indices that are sync
  samples. **If `stss` is absent, *every* sample is a keyframe** (intra-only content).
- **`ctts` — composition offsets.** The B-frame bridge (§5).

**The work of V1 is the join:** walk these compressed/cross-referenced tables and
expand them into one row per sample. Nothing downstream (segmenting, manifests) ever
touches a box again — they read your flat `Vec<Sample>`.

### Worked example: resolving one sample's byte offset

Say `stsc` says chunk 1 holds 3 samples, `stco` says chunk 1 starts at file offset
`0x9000`, and `stsz` says samples are `[5000, 1200, 800, …]` bytes.

| Sample | In chunk | Offset =                    | Value    |
|--------|----------|-----------------------------|----------|
| 0      | chunk 1  | `0x9000`                    | `0x9000` |
| 1      | chunk 1  | `0x9000 + 5000`             | `0xA388` |
| 2      | chunk 1  | `0x9000 + 5000 + 1200`      | `0xA838` |
| 3      | chunk 2  | `stco[1]` (new chunk base)  | …        |

That arithmetic — combining `stsc` + `stco` + running `stsz` sum — is exactly step 3
of the `demux()` TODO. Get it right and every `Sample.offset` points at real coded
bytes inside `mdat`.

---

## 5. The insight that separates a real demuxer from a toy: decode ≠ display

This is the concept most worth owning, and the one the SPEC's **Trap** warns about.

Modern video uses three frame types:

- **I-frame** (keyframe / IDR): references nothing. Decodes alone.
- **P-frame**: references an *earlier* frame ("predict from the past").
- **B-frame**: references frames *before and after* it ("bidirectional").

A B-frame needs a *future* frame to exist before it can be decoded. So the encoder
**stores frames in a different order than they're shown.** Classic example — you type
frames I, B, P but they must be stored P-before-B:

```
Display order (what you watch):   I    B    P
Decode/storage order (in file):   I    P    B
                                        └────┴─ P must decode before the B that
                                                references it
```

So each sample carries two timestamps:

- **decode time (DTS)** — running sum of `stts` deltas, the order in the file.
- **presentation time (PTS)** — when it's actually shown = `DTS + ctts_offset`.

`ctts` stores that per-sample offset. In the scaffold, `Sample.decode_time` and
`Sample.composition_offset` are separate fields precisely so you keep both, and the
comment spells it out: *`presentation_time = decode_time + composition_offset`*.

> **The trap, stated plainly:** if your test fixture has no B-frames, `ctts` is
> absent and PTS == DTS *by accident*. Assume that's always true and you'll ship a
> demuxer that silently scrambles presentation timing on every real-encoder file —
> which all use B-frames. The V1 criterion *"presentation vs decode order is
> preserved"* exists to force you to handle this even though the happy-path fixture
> might not show it.

---

## 6. What "keyframe" really means, and why V2/V3/V4 depend on it

A **sync sample** (`stss`) is a frame that references nothing earlier — you can start
decoding *there* with an empty decoder and get a correct picture. That's the whole
reason V1 must extract `is_sync` accurately:

- **V2 (segmenting)** may only cut a new segment *at a keyframe* — otherwise the
  segment can't decode standalone.
- **V4 (seeking / ABR)** can only jump to, or switch quality at, a keyframe.

So `stss` is *load-bearing* for the entire project. A wrong keyframe flag doesn't
error — it produces segments that look fine and fail to decode. Extract it carefully.

---

## 7. Why your parser must be *total* (never panic)

You are parsing **untrusted bytes**. A truncated upload, a bit-flip in transit, or a
deliberately malformed file must produce `Err(AppError::MalformedMedia)` — **never** a
panic, an integer overflow, or an out-of-bounds slice. This is a graded V1 criterion
and has its own property test (`malformed_input_never_panics`).

The discipline is one rule: **validate every length against the remaining buffer
*before* you index with it.**

```
A box claims size = 900000, but only 40 bytes remain in the buffer.
  Naive:  &data[pos .. pos + 900000]   → panic / OOB
  Total:  if size > remaining { return Err(MalformedMedia) }
```

The same check applies to a `size == 1` 64-bit header that claims there are 8 more
bytes to read the u64 from, a table `entry_count` that would run past the box, and a
sample offset that points outside the file. `bytes::Buf::get_u32/get_u64` read
big-endian (the box wire order) — but they *panic* if the buffer is too short, so you
check *first*. Every `todo!()` in `isobmff.rs` mentions this; it's not paranoia, it's
the spec of the function.

**Depth probe worth answering before you build:** *why must a progressive-download mp4
put `moov` before `mdat`?* If the index is at the **end**, a player streaming the file
top-to-bottom can't start until it has downloaded the *entire* media just to reach the
index. "Faststart" / "web-optimized" mp4 rewrites the file to move `moov` to the
front. Notice this is a *layout* fix on the same boxes — and it's exactly the
limitation V2's fragmentation solves at a deeper level.

---

## 8. In the wild

Everything reads this box tree first: `ffprobe`, `mp4box -info`, Bento4, Shaka
Packager, and every browser/player demuxer. The same format underlies `.mov`, CMAF
(what you'll *emit* in V2), and even HEIF images. You are hand-writing the layer
`ffmpeg`'s demuxer would normally hand you — which is the point of the rung.

---

## Mental model summary

| Thing | One-liner |
|-------|-----------|
| Box | length-prefixed `[size][fourcc][payload]`; skip-by-length = forward-compatible |
| `moov` vs `mdat` | index vs media; the tree exists to find these two |
| `stbl` tables | each encodes one dimension (timing / size / geometry / keyframes), compressed |
| Sample table | your flat `Vec<Sample>` — the join of all those tables |
| DTS vs PTS | decode order (file) vs display order; `ctts` bridges them; B-frames create the gap |
| Sync sample | decodes alone; the only legal segment/seek boundary |
| Totality | check length before index → `Err`, never panic |

## Where you'll build this

[`src/isobmff.rs`](../src/isobmff.rs):
- `read_box_header()` — the `[size][type]` parse with 32/64-bit sizes + bounds checks.
- `demux()` — the descent + the table join into `Vec<Sample>` per `Track`.

**This doc unlocks these V1 "Done when ALL true" boxes:** per-track timescale/codec +
full sample table; sample count & duration match the source; `stco` **and** `co64`
handled; `ctts` applied (PTS recoverable); codec init data (`avcC`/SPS/PPS) retained;
malformed input rejected without panic.

**When you hit the interesting decisions** — how to structure the recursive descent,
how exactly to expand `stsc` into per-sample chunk membership, how to keep the parser
total without drowning in bounds checks — that's the build, not this doc. Reach for
[`/hint`](../../..) for a graduated nudge and [`/quest`](../../..) for a guided,
test-first session over V1.
