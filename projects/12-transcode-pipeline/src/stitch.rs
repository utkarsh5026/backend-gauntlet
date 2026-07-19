//! V4 — Stitch + remux: glue the independently-transcoded chunks back into one
//! continuous file. This is the fan-in — the "reduce" to V1's "map".
//!
//! Each chunk was encoded on its own worker, in its own process, with its own
//! timeline starting at zero. Concatenating them naively — byte-appending, or
//! trusting each chunk's timestamps — produces the signature bug of distributed
//! transcoding: a **seam**. At every chunk boundary you get a timestamp
//! discontinuity, a duplicated or dropped frame, or audio drift, because chunk
//! *N+1*'s decode times restart instead of continuing from where chunk *N* ended.
//!
//! Stitching correctly means: order the chunks (numeric index order — that's why
//! V1 numbered them), and produce an output whose presentation timestamps are
//! **monotonic and gapless across every boundary**, whose total duration matches
//! the source within a frame, and whose A/V stays in sync. Because the boundaries
//! are keyframe-aligned (V1), each chunk decodes standalone, so this is a *remux*
//! (rewrap, rebasing timestamps) — not a re-encode — which is what keeps it fast
//! and lossless.
//!
//! With this `todo!()`, a `Stitch` task panics — that panic is the V4 worklist.

use std::path::Path;

use crate::error::AppError;

/// Concatenate + remux every transcoded chunk in `chunk_dir` into `out`.
///
/// The inputs are the chunk files produced by V3's transcode tasks, named by chunk
/// index (`0.mp4`, `1.mp4`, …). `out` is the finished rendition file.
///
/// TODO(V4): stitch seamlessly.
///   - Collect the chunk files and order them by numeric index (not lexicographic
///     — `10.mp4` must not sort before `2.mp4`).
///   - Concatenate + remux them into `out` so that presentation timestamps are
///     **continuous and monotonic across boundaries** (rebase chunk *N+1*'s
///     timeline to start where chunk *N* ended — no reset, no gap, no overlap).
///     Prefer a remux (stream copy) over a re-encode: the chunks are already at the
///     target codec/bitrate, and re-encoding at the seam both wastes time and adds
///     a generation of loss. `ffmpeg::run(ffmpeg_bin, &args)` is your hammer.
///   - Write to a temp path and atomically rename to `out`, so the stitch is
///     idempotent (a re-run after a crash is safe) and a partial file is never
///     published.
///   - The result's total duration must equal the summed chunk durations (≈ the
///     source duration) within one frame, and A/V must stay in sync.
pub async fn stitch(ffmpeg_bin: &str, chunk_dir: &Path, out: &Path) -> Result<(), AppError> {
    let _ = (ffmpeg_bin, chunk_dir, out);
    todo!("V4: order chunks by index, concat+remux with continuous PTS, commit atomically")
}

#[cfg(test)]
mod tests {
    // TODO(V4): prove the stitch:
    //   - chunk files are ordered numerically, so `10.mp4` follows `9.mp4`;
    //   - the stitched output's duration equals the sum of chunk durations within
    //     one frame (no drift accumulates across many boundaries);
    //   - `ffprobe` reports monotonic, gapless presentation timestamps across every
    //     chunk boundary — no backwards jump, no gap (the "seam" is gone);
    //   - re-running the stitch reproduces a byte-identical (or ffprobe-identical)
    //     output — idempotent.
}
