"""V1 — Keyframe-aligned chunking: decide *where* to cut.

To transcode a video in parallel you slice it into chunks, encode each chunk on a
different worker, then glue the results back together. The whole scheme lives or
dies on **where** you cut: a decoder can only start at a **keyframe** (an IDR frame
that depends on nothing before it). Cut mid-GOP and the first frames of the chunk
reference frames that aren't there — the chunk can't decode standalone, the
re-encode produces garbage at the seam, and stitching (V4) fails. So a chunk
boundary may only fall on a keyframe.

Given the source's keyframe timestamps (from `ffmpeg.probe_keyframes`) and its
total duration, produce chunks whose boundaries are all keyframes and whose lengths
cluster around a target — the target is a *goal*, the keyframe boundary is the
*law*.

This module is pure arithmetic over timestamps — no media bytes, no ffmpeg, no
clock, no filesystem — so it is exhaustively property-testable, which is exactly
what the SPEC asks for. Keep it that way: the moment `plan_chunks` touches I/O it
stops being a function hypothesis can call ten thousand times a second.

## Proving it

The Proof is property tests over random ascending keyframe lists and durations
(hypothesis is already a dev dependency), e.g. in `tests/test_chunk.py`:

* every boundary — each chunk's `start`, and every `end` but the last — is a member
  of `keyframes`; no cut falls off a keyframe (`test_chunks_are_keyframe_aligned`);
* chunks are gapless and cover exactly `[0.0, duration)` with no overlap
  (`test_chunks_cover_source`);
* indices are `0..n` in ascending time order;
* no chunk exceeds `target_secs` *unless* it is a single GOP that already does (the
  keyframe boundary is allowed to win over the target);
* pathological inputs — one keyframe, keyframes past `duration`, a target larger
  than the whole asset — return one valid chunk, never an exception.

Two hypothesis details that bite on this input shape: `st.floats()` generates `nan`
and `inf` unless you forbid them, and a list you sort can still hold duplicates —
decide whether a repeated keyframe timestamp is an input you accept or one you
reject, and make the strategy say so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["ChunkPlan", "plan_chunks"]


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """One planned chunk: the half-open time span `[start, end)`, in seconds.

    `start` and — for every chunk but the last — `end` are keyframe timestamps.

    A frozen dataclass rather than a pydantic model: a plan never crosses a wire or
    the database (the DAG stores only its `index`), and the property tests build
    hundreds of thousands of them, where validation would be pure overhead.
    """

    index: int
    """0-based chunk index — becomes `Transcode.chunk` and the chunk artifact's file
    name, so stitch order (V4) is just numeric order."""
    start: float
    """Start time in seconds (a keyframe timestamp; chunk 0 starts at 0.0)."""
    end: float
    """End time in seconds (the next chunk's `start`; the last chunk ends at the
    source duration)."""

    @property
    def seconds(self) -> float:
        """Length of this chunk in seconds."""
        return self.end - self.start


def plan_chunks(keyframes: Sequence[float], duration: float, target_secs: float) -> list[ChunkPlan]:
    """Group a source into keyframe-aligned chunks of ~`target_secs`.

    `keyframes` is the ascending list of keyframe timestamps (seconds) from
    `ffmpeg.probe_keyframes`; `duration` is the source length in seconds.

    TODO(V1): the chunking policy.
      - Walk the keyframes. A chunk may only *begin* on a keyframe. Extend the
        current chunk keyframe by keyframe until taking the next GOP would push its
        length past `target_secs`; cut at that keyframe and start the next chunk.
        (Whether you cut just before or just after the target is your call —
        record it in `docs/12-design.md`.)
      - Never cut anywhere but a keyframe, so real lengths cluster *around* the
        target, not on it.
      - Gapless and total: `chunks[n + 1].start == chunks[n].end`, the first starts
        at `0.0`, the last ends at `duration`. That `==` is exact float equality,
        and it holds only if the next start *is* the previous end — the same value
        carried across, never recomputed as `start + length`, which can differ in
        the last bits.
      - Index chunks `0..n` in time order.
      - Degenerate inputs must not raise: a single keyframe (or none usable beyond
        the start) yields one chunk `[0.0, duration)`.

    A linear walk is fine at feature length — a 2-hour source with a 2 s GOP has
    3,600 keyframes. If you'd rather *jump* ("the last keyframe at or before
    `start + target_secs`"), `bisect` answers that over a sorted list in O(log n)
    without a hand-written binary search.
    """
    raise NotImplementedError("V1: cut into keyframe-aligned, gapless chunks of ~target_secs")
