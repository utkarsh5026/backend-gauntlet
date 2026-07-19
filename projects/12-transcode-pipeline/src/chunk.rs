//! V1 — Keyframe-aligned chunking: decide *where* to cut.
//!
//! To transcode a video in parallel you slice it into chunks, encode each chunk on
//! a different worker, then glue the results back together. The whole scheme lives
//! or dies on **where** you cut: a decoder can only start at a **keyframe** (an IDR
//! frame that depends on nothing before it). Cut mid-GOP and the first frames of
//! the chunk reference frames that aren't there — the chunk can't decode standalone,
//! the re-encode produces garbage at the seam, and stitching (V4) fails. So a chunk
//! boundary may only fall on a keyframe.
//!
//! Given the source's keyframe timestamps (from `ffmpeg::probe_keyframes`) and the
//! total duration, produce a set of chunks whose boundaries are all keyframes and
//! whose lengths cluster around a target — the target is a *goal*, the keyframe
//! boundary is the *law*.
//!
//! This module is pure arithmetic over timestamps — no media bytes, no ffmpeg — so
//! it's exhaustively property-testable, which is exactly what the SPEC asks for.

use serde::{Deserialize, Serialize};

/// One planned chunk: a half-open time span `[start, end)` in seconds. `start` and
/// (for every chunk but the last) `end` are keyframe timestamps.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ChunkPlan {
    /// 0-based chunk index — becomes the `Transcode { chunk }` task id and the
    /// chunk artifact's name, so the stitch order (V4) is just numeric order.
    pub index: u32,
    /// Start time in seconds (a keyframe timestamp; chunk 0 starts at 0.0).
    pub start: f64,
    /// End time in seconds (the next chunk's `start`; the last chunk ends at the
    /// source duration).
    pub end: f64,
}

impl ChunkPlan {
    /// Length of this chunk in seconds.
    pub fn seconds(&self) -> f64 {
        self.end - self.start
    }
}

/// Group a source into keyframe-aligned chunks of ~`target_secs`.
///
/// `keyframes` is the ascending list of keyframe timestamps (seconds) from
/// `ffmpeg::probe_keyframes`; `duration` is the source length in seconds.
///
/// TODO(V1): the chunking policy.
///   - Walk the keyframes. A chunk may only *begin* on a keyframe. Extend the
///     current chunk keyframe-by-keyframe until adding the next GOP would push its
///     length past `target_secs`; cut at that keyframe and start the next chunk.
///   - Never cut anywhere but a keyframe — real chunk lengths therefore cluster
///     around, not exactly on, the target.
///   - Chunks must be **gapless and total**: `chunk[n+1].start == chunk[n].end`,
///     the first starts at `0.0`, and the last ends at `duration` — every frame of
///     the source belongs to exactly one chunk.
///   - Index chunks `0..n` in order.
///   - Degenerate inputs must not panic: a source with a single keyframe (or none
///     usable beyond the start) yields one chunk `[0.0, duration)`.
///
/// Keep this a pure function of its inputs (no clock, no fs) so it stays
/// deterministic and property-testable.
pub fn plan_chunks(keyframes: &[f64], duration: f64, target_secs: f64) -> Vec<ChunkPlan> {
    let _ = (keyframes, duration, target_secs);
    todo!("V1: cut into keyframe-aligned, gapless chunks of ~target_secs")
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the planner, e.g. as property tests over random ascending
    // keyframe lists + durations:
    //   - every boundary (each chunk's `start`, and every `end` but the last) is a
    //     member of `keyframes` — no cut falls off a keyframe;
    //   - chunks are gapless and cover exactly `[0.0, duration)` with no overlap;
    //   - indices are `0..n` in ascending time order;
    //   - no chunk exceeds `target_secs` *unless* it is a single GOP that already
    //     does (the keyframe boundary is allowed to win over the target);
    //   - pathological inputs (one keyframe, keyframes past `duration`, target
    //     larger than the whole asset) return one valid chunk, never a panic.
}
