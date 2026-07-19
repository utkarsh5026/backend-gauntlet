# Keyframes, GOPs, and Where to Cut — From First Principles

> A ground-up guide to why you can't slice a video just anywhere, what a GOP
> actually is, and why the chunk *plan* — pure arithmetic over timestamps — is the
> most load-bearing hundred lines in this whole pipeline. No prior knowledge of
> video codecs assumed.
>
> This prepares you for **V1 (keyframe-aligned chunking)** in
> [SPEC.md](../SPEC.md). The function you'll write is
> [`plan_chunks`](../src/chunk.rs) in [src/chunk.rs](../src/chunk.rs) — currently a
> `todo!()`. Its inputs come from the already-wired
> [`ffmpeg::probe_keyframes`](../src/ffmpeg.rs) and
> [`ffmpeg::probe_duration`](../src/ffmpeg.rs). This doc teaches the concept and
> the invariants; it does **not** write the policy for you — that's the vertical.

---

## 0. The one sentence to hold onto

**A decoder can only start at a keyframe, so a chunk boundary may fall *only* on a
keyframe timestamp — the target chunk length is a preference, the keyframe
boundary is the law.**

Everything in V1 is a consequence of that sentence.

---

## 1. Why we're cutting at all

A two-hour movie is 7,200 seconds of video. A software H.264 encode of good
quality typically runs somewhere around realtime on one machine — call it 1×. The
job isn't one encode, it's a **ladder**: 1080p + 720p + 480p means roughly

```
7,200 s  ×  3 renditions  ≈  6 hours of serial encoding
```

for a movie that takes 2 hours to *watch*. No one ships that. The escape hatch is
the same one MapReduce uses for logs:

```
          split                 map (parallel)              reduce
  source ───────▶ chunks ───▶ transcode each chunk ───▶ stitch per rendition
```

Cut the source into ~200 chunks, transcode them on 8+ workers at once, glue the
results back. The SPEC calls this "map/reduce for video", and it's exactly what
AWS MediaConvert, YouTube ingestion, and Mux do internally.

But video is not a log file. You can split a log at any newline. Where can you
split a video?

---

## 2. Video frames reference other frames

The naive mental model — "a video is a sequence of pictures" — is wrong in the one
way that matters here. Storing 30 full pictures per second would be enormous, and
consecutive frames are nearly identical. So codecs store most frames as **deltas
against other frames**:

| Frame type | What it stores | What it needs to decode |
| --- | --- | --- |
| **I-frame** (intra) | A complete picture, self-contained | Nothing — decodes alone |
| **P-frame** (predicted) | Differences from an *earlier* frame | That earlier frame, already decoded |
| **B-frame** (bi-predicted) | Differences from earlier *and later* frames | Both reference frames |

Frames are organized into **GOPs** (groups of pictures): each GOP opens with an
I-frame, followed by a run of P- and B-frames that ultimately chain back to it.
A typical 2-second GOP at 30 fps:

```
time ──▶
  I  B  B  P  B  B  P  B  B  P ...  I  B  B  P ...
  └──────────── GOP (~60 frames) ───┘└── next GOP ──
  ▲                                  ▲
  keyframe                           keyframe
```

Every P and B frame in the GOP is meaningless without the frames it references.
Only the I-frame at the top stands alone.

One more refinement, because the SPEC's concept list names it: not every I-frame
is a safe entry point. An **IDR** frame (Instantaneous Decoder Refresh) is an
I-frame with a guarantee attached: *no later frame references anything before it*.
A plain (non-IDR) I-frame permits later frames to reach back across it — that's an
**open GOP**, and cutting at such an I-frame still leaves dangling references. A
**closed GOP** is one no frame reaches across. When this doc (and the code) says
"keyframe", it means an IDR — the wired
[`probe_keyframes`](../src/ffmpeg.rs) asks ffprobe for exactly the frames a
decoder may start at (`-skip_frame nokey`).

---

## 3. What happens if you cut anywhere else

Say the source has keyframes at `0.0, 2.0, 4.0, …` and you cut a chunk at
`t = 5.0` — mid-GOP, one second past the keyframe at `4.0`. The chunk's first
frames are P/B frames whose references live *before* `5.0`, in the previous
chunk. Concretely:

| Failure | Mechanism | When you find out |
| --- | --- | --- |
| Chunk can't decode standalone | First frames are deltas against frames the chunk doesn't contain | When a worker's ffmpeg errors — *if you're lucky* |
| Silent garbage at the seam | Many decoders don't error; they decode against grey/stale reference data and emit smeared, blocky frames | Hours later, watching the stitched output at minute 37 |
| Duplicate or missing frames | The cutter seeks to the *nearest* keyframe instead of your cut point, so adjacent chunks overlap or gap | When V4's gapless-total check fails — or never |
| Wasted compute at scale | 200 chunks × 3 renditions re-encoded before anyone looks at pixels | After the whole job "succeeds" |

The trap is that most of these are **silent**. `ffmpeg` exits 0; the file exists;
the duration looks right. The corruption is visual, at boundaries, in the middle
of a two-hour asset nobody will scrub through. This is why the SPEC insists the
cut plan be provably correct *by arithmetic*, before any media is touched.

---

## 4. The plan is pure math over timestamps

Here is the design win the concept card asks you to internalize:
**deciding where to cut requires no media I/O at all.** The probe (wired for you)
reduces the entire 4 GB source to two small values:

- `keyframes: Vec<f64>` — ascending timestamps of every safe entry point
- `duration: f64` — total length in seconds

From there, [`plan_chunks(keyframes, duration, target_secs)`](../src/chunk.rs) is
a **pure function** — no clock, no filesystem, no ffmpeg. That purity buys you:

1. **Exhaustive testing.** Property tests can throw thousands of random keyframe
   lists at it per second. An encode-based test takes minutes per case and tells
   you *that* something broke, not *which invariant* broke.
2. **Determinism.** The same source always yields the same plan — which V3's
   idempotency story will lean on (a re-run split must produce identical chunks).
3. **Cheap scheduling.** The DAG (V2) can be expanded from the plan alone; no
   worker touches the source until the transcodes run.

### A worked example

Source: duration `13.0` s, keyframes every 2 s: `[0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]`,
target `5.0` s. One *valid* plan (yours may differ — the policy is your decision):

```
keyframes:   0.0   2.0   4.0   6.0   8.0   10.0   12.0        13.0 (duration)
              │     │     │     │     │      │      │            │
chunks:       ├──── chunk 0 ────┤─── chunk 1 ───────┤─ chunk 2 ──┤
              [0.0 ──────▶ 6.0) [6.0 ─────▶ 12.0)   [12.0 ─▶ 13.0)
              len 6.0           len 6.0             len 1.0
```

Check it against the invariants:

| Invariant (SPEC "Done when") | In this plan |
| --- | --- |
| Every boundary is a keyframe | `0.0`, `6.0`, `12.0` are all members of `keyframes`; the final `13.0` is the duration (the one non-keyframe end allowed) |
| Gapless and total | `chunk[1].start == chunk[0].end`, first starts `0.0`, last ends `13.0` — every frame in exactly one chunk |
| Lengths cluster around target | 6.0, 6.0, 1.0 around a 5.0 target — a boundary *can't* land on 5.0, so the plan overshoots to the keyframe |
| Ascending indices `0..n` | `index` is 0, 1, 2 in time order — V4 will stitch by this number alone |

Notice what the target being "a goal, not a law" means concretely: no chunk here
is 5.0 s long, and that's *correct*. If the source had a single 20-second GOP
(keyframes at `0.0` and `20.0` only), the plan would contain a 20-second chunk —
the keyframe boundary wins, and the SPEC requires that tradeoff to be visible in
the plan, not silently "fixed" by cutting mid-GOP.

### The degenerate cases

The SPEC calls these out because they're where naive loops panic or emit garbage:

| Input | Required output |
| --- | --- |
| One keyframe (`[0.0]`), duration 42.0 | One chunk `[0.0, 42.0)` |
| No keyframe usable past the start | One chunk `[0.0, duration)` |
| `target_secs` > duration | One chunk `[0.0, duration)` |
| Keyframes listed *past* the duration | Never a boundary beyond `duration`, still gapless and total |

The rule of thumb: any input collapses to *at least one valid chunk*, never a
panic, never an empty plan for a non-empty source.

---

## 5. The design space (yours to decide)

The scaffold's doc comment on [`plan_chunks`](../src/chunk.rs) sketches the shape
of a greedy walk, but several decisions are genuinely yours, and
`docs/12-design.md` must record the policy you pick:

- **Overshoot or undershoot?** When adding the next GOP would push a chunk past
  the target, do you cut *before* it (chunk slightly under target) or *after*
  (slightly over)? Both can satisfy every invariant; they produce different
  distributions of chunk lengths. Which clusters closer to the target for your
  keyframe cadence?
- **Chunk count vs chunk size.** Keyframes every 2 s with a 30 s target gives
  240 chunks for a 2-hour source; a 6 s target gives 1,200. More chunks = finer
  parallelism and smaller straggler cost (V3's boss fight), but more per-task
  overhead (process spawn, DB rows, claim round-trips) and more seams for V4 to
  get right. Where's the knee? The default lives in
  [`PipelineConfig::target_chunk_secs`](../src/job.rs).
- **The last sliver.** A source ending 0.3 s after its final keyframe produces a
  tiny tail chunk. Merge it into its neighbor or keep it? Either is valid if the
  invariants hold — but decide on purpose.

When you can articulate why you chose each of these, you're ready to write the
`todo!()`. If you get stuck, `/hint` gives graduated nudges and `/quest` runs the
guided build — this doc deliberately stops at the door.

---

## 6. Mental model summary

| Concept | The one-liner |
| --- | --- |
| I/P/B frames | Most frames are deltas; only I-frames stand alone |
| GOP | A keyframe plus the delta-frames that chain back to it |
| IDR / closed GOP | A keyframe *nothing later reaches across* — the only safe cut point |
| Open GOP | Frames reference across a keyframe — why "I-frame" ≠ "safe boundary" |
| The plan | Pure arithmetic: keyframe timestamps + duration → gapless, total, keyframe-aligned chunks |
| Target vs boundary | Target is a goal; the keyframe boundary is the law; overshoot is correct |
| Why purity matters | Property-testable in milliseconds, deterministic for idempotency, schedulable without media I/O |

## 7. Where you'll build this

- **Module:** [src/chunk.rs](../src/chunk.rs) — the `todo!()` in
  [`plan_chunks`](../src/chunk.rs), plus the property tests sketched in its
  `#[cfg(test)]` block (`prop_chunks_are_keyframe_aligned`,
  `prop_chunks_cover_source`).
- **Unlocks (V1 "Done when ALL true"):** boundaries-are-keyframes ·
  gapless-and-total coverage · lengths cluster around `target_secs` with GOP
  overshoot allowed · ascending `0..n` indices · degenerate inputs yield one
  valid chunk, never a panic.
- **Feeds:** V2's `dag::expand` consumes the plan's chunk count; V3 transcodes
  each `[start, end)` span; V4's seamless remux is only *possible* because these
  boundaries are keyframes.
