# Stitching: The Seamless Join — From First Principles

> A ground-up guide to why independently-encoded chunks refuse to glue together
> cleanly, what a presentation timestamp actually is, and why the fix is a
> *remux* (rewrap + rebase) rather than another encode. No prior container-format
> knowledge assumed.
>
> This prepares you for **V4 (stitch + remux)** in [SPEC.md](../SPEC.md). You'll
> write [`stitch`](../src/stitch.rs) in [src/stitch.rs](../src/stitch.rs) — the
> "reduce" that joins V3's fan-out, invoked by the wired `Stitch` arm of
> [`Worker::execute`](../src/worker.rs) over the files in
> [`chunk_dir`](../src/job.rs), landing at
> [`rendition_output`](../src/job.rs). This doc teaches why seams exist and what
> "seamless" means observably; the concat method is yours to choose.

---

## 0. The one sentence to hold onto

**Each chunk was encoded by its own process with its own timeline starting at
zero, so joining them is a *timestamp* problem, not a pixel problem: offset every
chunk's clock by the running sum of the durations before it, and the seam
disappears without touching a single frame.**

---

## 1. Where the seam comes from

V3 left you a directory per rendition:

```
WORK_DIR/jobs/{id}/720p/chunks/
  0.mp4   1.mp4   2.mp4   …   199.mp4
```

Every media file carries, alongside its compressed frames, a **presentation
timestamp (PTS)** per frame: "show this frame at t = X". A player doesn't play
frames "as fast as they come" — it schedules them against these clocks. (There's
a sibling, the decode timestamp/DTS, because B-frames decode out of display
order; MP4 stores the same idea per-fragment as `baseMediaDecodeTime`. Same
concept: embedded clocks.)

Here's the problem. Each chunk was produced by a separate ffmpeg process that
knew nothing about the others — so **every chunk's clock starts at zero**:

```
what the chunks say:          0.mp4: 0──────6    1.mp4: 0──────6    2.mp4: 0───4
                                     PTS 0..6           PTS 0..6         PTS 0..4

naive concat's timeline:      0──────6 0──────6 0───4
                                      ▲        ▲
                                      PTS jumps backwards to 0 at every boundary
```

What a player does when time runs backwards is undefined-by-implementation:
stutter, freeze-and-resync, dropped frames, an audio pop as its clock snaps.
That artifact — at *every* chunk boundary — is the **seam**, the signature bug of
distributed transcoding.

What the joined file must say instead:

```
target timeline:              0──────6──────12───16
                              chunk 0 chunk 1 chunk 2
                              offset  offset  offset
                              +0      +6      +12
```

Chunk N's frames are all shifted by **the sum of the durations of chunks
0..N** — that's the rebasing rule the SPEC wants recorded in
`docs/12-design.md`. The frames themselves are untouched; only the clocks move.

## 2. The sort bug that ships

The second, dumber seam: ordering. The chunk files are named by V1's numeric
index, but directory listings are strings, and strings sort lexicographically:

```
numeric order:        0, 1, 2, …, 9, 10, 11, …
lexicographic order:  0, 1, 10, 11, …, 19, 2, 20, …
```

(Verified: `sorted(["0","1","2","9","10","11"])` → `0, 1, 10, 11, 2, 9`.)

A test asset with ≤ 9 chunks passes every check; the first 10+-chunk source
plays minute 10 before minute 2. This is why the SPEC gives numeric ordering its
own criterion and test (`chunks_ordered_numerically`) — and why V1 bothered to
guarantee ascending indices: **stitch order is numeric order, nothing else.**

## 3. Remux, not re-encode

Two ways to make one file out of 200:

| | Re-encode stitch | **Remux stitch** |
| --- | --- | --- |
| What it does | Decode all chunks, re-encode as one stream | Copy compressed frames, rewrite containers/timestamps |
| Quality | A *second* generation of lossy encoding on every frame | Bit-exact frames — zero added loss |
| Speed | Another full-length encode — the serial cost you parallelized to avoid | Roughly I/O speed |
| CPU | The whole job again, serially, at the join | Trivial |

The remux is only *possible* because of V1: every chunk begins at a keyframe, so
its frames decode standalone and are valid to place back-to-back — no dangling
references at the joins. Cut off-keyframe and no stitch, however clever, can
remux the pieces seamlessly; you'd be forced into the re-encode column. (The
concept card: *the whole scheme is downstream of V1.*)

`ffmpeg` exposes more than one concatenation mechanism (a concat **demuxer**, a
concat **protocol**, and a concat **filter**), and they differ in exactly the
dimensions of the table above — which streams they decode, and what they do to
timestamps. Choosing among them, and verifying your choice actually rebases
rather than resets the clocks, is the heart of the vertical. This doc stops
here; `/hint` if you get stuck.

## 4. Drift: why "within one frame" is the spec

Durations are stored in integer **timebase ticks**, not exact seconds, so each
chunk's "duration" can carry a sub-millisecond rounding error. Per boundary
that's invisible. But the rebasing offset is a *running sum*: an error of just
1 ms per boundary across 300 chunks accumulates to **300 ms** by the end of a
feature — video visibly ahead of audio, or a duration check off by seconds.

Hence the SPEC's criteria are phrased as *end-to-end* invariants, not
per-boundary ones:

- total duration = source duration **within one frame** (33.3 ms at 30 fps,
  41.7 ms at 24 fps) — drift has nowhere to hide;
- PTS **monotonic and gapless across every boundary** — each seam individually
  clean;
- **A/V in sync** throughout — the two streams' accumulated errors can't diverge.

The design question to sit with: does your method accumulate error (sum of 300
rounded durations) or not (each offset derived from a single exact source of
truth)? That decision belongs in `docs/12-design.md`.

## 5. Audio doesn't share your boundaries

One honest complication the concept card's depth probe raises. Audio is encoded
in fixed-size frames — AAC uses 1024 samples per frame, which at 48 kHz is
21.33 ms (23.22 ms at 44.1 kHz). Video frames at 30 fps are 33.33 ms. These
grids don't align, and neither aligns with V1's keyframe cuts:

```
video frames: |––33.3––|––33.3––|––33.3––|––33.3––|
audio frames: |–21.3–|–21.3–|–21.3–|–21.3–|–21.3–|
cut at t=6.0:                 ▲
                    lands mid-audio-frame, always
```

So a chunk's audio is up to ~one audio frame longer or shorter than its video,
and per-chunk A/V starts may differ by a few ms. A stitch that rebases only one
stream's clock, or assumes both streams have identical chunk durations, drifts
A/V apart seam by seam. This is *why* "A/V stays in sync across boundaries" is
its own criterion rather than being implied by the video checks — verify both
streams, not just `v:0`.

## 6. Verification: ffprobe is the test, eyes are the fallback

The card's trap: a half-second seam at minute 37 of the 480p rendition will
never be caught by watching. "No seam" must be *machine-checkable*, and it is —
every property above is observable with ffprobe (the same tool
[`probe_keyframes`](../src/ffmpeg.rs) already wraps: frame-level
`pts_time` dumps and `format=duration` are all you need). The SPEC's tests:

| Test | Asserts |
| --- | --- |
| `stitched_output_has_no_seam` | PTS monotonic + gapless across every boundary |
| `stitched_duration_matches_source` | Total duration within one frame of the source |
| `chunks_ordered_numerically` | `10.mp4` follows `9.mp4` |
| (idempotency) | Re-running the stitch reproduces identical output |

That last row: the stitch is a task like any other, run under V3's at-least-once
regime — a lease can expire mid-stitch and a second stitch can run. Same
discipline as doc 02: write to a temp path, `rename` into
[`rendition_output`](../src/job.rs), deterministic output. A crashed stitch must
never publish a partial `out.mp4` — that file is the *product*, the thing
project 11 packages.

## 7. Mental model summary

| Concept | The one-liner |
| --- | --- |
| PTS / embedded clocks | Frames carry "show me at t=X"; players schedule against it, so backwards time = visible seam |
| Why chunks disagree | Independent encoders, each timeline starts at 0 — the fan-out's price, paid at the fan-in |
| Rebasing | Chunk N's offset = sum of durations of chunks 0..N; move clocks, not pixels |
| Numeric order | `10.mp4` after `9.mp4` — lexicographic sorting scrambles any 10+-chunk asset |
| Remux vs re-encode | Copy frames + rewrite timestamps: no quality loss, no serial encode; possible only because V1 cut on keyframes |
| Drift | Rounding × 300 boundaries = visible desync; end-to-end duration-within-a-frame is the guard |
| A/V misalignment | 1024-sample audio frames don't land on video cuts — sync is a per-stream obligation |
| Verification | ffprobe invariants are the test; watching is the fallback |

## 8. Where you'll build this

- **Module:** [src/stitch.rs](../src/stitch.rs) — the `todo!()` in
  [`stitch`](../src/stitch.rs) (order numerically → concat/remux with continuous
  PTS → temp→rename), with [`ffmpeg::run`](../src/ffmpeg.rs) as the hammer and
  the test sketch in its `#[cfg(test)]` block.
- **Unlocks (V4 "Done when ALL true"):** numeric join order · monotonic, gapless
  PTS across every boundary · total duration within one frame · A/V sync held ·
  remux-not-re-encode, idempotent + atomic.
- **Feeds:** the finished `out.mp4` per rendition is what `POST /jobs` promised —
  hand it to project 11's packager. The boss fight's "seamless output" gate is
  this doc's invariants under load, after a worker was murdered mid-job.
