"""V4 — Stitch + remux: glue the independently transcoded chunks back into one
continuous file. This is the fan-in — the "reduce" to V1's "map".

Each chunk was encoded on its own worker, in its own process, with its own timeline
starting at zero. Concatenating them naively — byte-appending, or trusting each
chunk's timestamps — produces the signature bug of distributed transcoding: a
**seam**. At every chunk boundary you get a timestamp discontinuity, a duplicated or
dropped frame, or audio drift, because chunk *N+1*'s timestamps restart instead of
continuing from where chunk *N* ended.

Stitching correctly means: order the chunks (numeric index order — that's why V1
numbered them), and produce an output whose presentation timestamps are **monotonic
and gapless across every boundary**, whose total duration matches the source within
a frame, and whose A/V stays in sync. Because the boundaries are keyframe-aligned
(V1), each chunk decodes standalone, so this is a *remux* (rewrap, rebasing
timestamps) — not a re-encode — which is what keeps it fast and lossless.

With this unbuilt, a `Stitch` task raises — that is the V4 worklist.

## Proving it

* chunk files are ordered numerically, so `10.mp4` follows `9.mp4`
  (`test_chunks_ordered_numerically`);
* the stitched output's duration equals the sum of chunk durations within one frame
  — no drift accumulates across many boundaries
  (`test_stitched_duration_matches_source`);
* `ffprobe` reports monotonic, gapless presentation timestamps across every chunk
  boundary — no backwards jump, no gap (`test_stitched_output_has_no_seam`);
* re-running the stitch reproduces a byte-identical (or ffprobe-identical) output.

Real chunks cost nothing to make in a test: ffmpeg's `lavfi` input generates video
(`testsrc2`) and audio (`sine`) on the fly — `make fixture` shows the arguments — and
the `ffmpeg_tools` fixture in `tests/conftest.py` skips such a test on a machine
without ffmpeg rather than failing it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["stitch"]


async def stitch(ffmpeg_bin: str, chunk_dir: Path, out: Path) -> None:
    """Concatenate + remux every transcoded chunk in `chunk_dir` into `out`.

    The inputs are the chunk files V3's transcode tasks produced, named by chunk
    index (`0.mp4`, `1.mp4`, …). `out` is the finished rendition.

    TODO(V4): stitch seamlessly.
      - Collect the chunk files and order them by **numeric** index. `sorted()` over
        names or `Path`s is lexicographic — `"10.mp4" < "2.mp4"` — so order by the
        integer each name encodes, and decide what a name that isn't one means.
      - Concatenate + remux into `out` so presentation timestamps are **continuous
        and monotonic across boundaries**: chunk N+1's timeline starts where chunk
        N's ended — no reset, no gap, no overlap. Prefer a remux (stream copy) over a
        re-encode: the chunks are already at the target codec and bitrate, and
        re-encoding adds a generation of loss. `ffmpeg.run(ffmpeg_bin, args)` is the
        hammer; ffmpeg has more than one kind of "concat", and they treat timestamps
        differently.
      - Write to a temp path **in the same directory** as `out`, then `os.replace` it
        into place. A rename is atomic only within one filesystem, and `os.replace`
        overwrites an existing `out` — which a re-run after a crash has to do.
      - Total duration must equal the summed chunk durations (≈ the source) within
        one frame, and audio must stay in sync with video.

    Listing a directory of 1,200 chunks is synchronous filesystem I/O; done on the
    event loop it stalls every other worker for as long as it takes.
    `asyncio.to_thread` moves it off.
    """
    raise NotImplementedError(
        "V4: order chunks by index, concat+remux with continuous PTS, commit atomically"
    )
