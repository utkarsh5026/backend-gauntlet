"""V2 — The fMP4 / CMAF segmenter: write the boxes by hand.

Module: `src/vod_streaming/segment.py`. This turns V1's sample table into a
**CMAF init segment** plus a run of **keyframe-aligned media segments** — the
marquee vertical, and the only place in this project where you are a *writer* of
the container rather than a reader of it.

Two outputs:

* **init segment** = `ftyp` + `moov` carrying the codec setup and *zero* samples.
  One per rendition, identical for every media segment that follows it.
* **media segment** = `styp` + `moof` + `mdat`. The `moof` (`mfhd` + `traf`
  [`tfhd` + `tfdt` + `trun`]) describes the fragment: `tfdt`'s
  `baseMediaDecodeTime` anchors it on the timeline, and `trun` lists each
  sample's size, duration and composition offset. The `mdat` is the coded bytes
  copied out of the source at the offsets V1 recorded.

The rule that makes the whole scheme work: **every segment starts on a keyframe**.
The target duration is a goal, not a law — accumulate GOPs until the next one
would push you past the target, then cut. A segment that cannot be decoded
standalone is the bug this vertical exists to prevent, and it is invisible until
a player tries to start playback in the middle of your asset.

## Writing binary in Python without making it slow or wrong

**Build into a `bytearray`, or collect and `join`.** `buf += struct.pack(...)`
into a `bytearray` is amortized O(1); the same loop over immutable `bytes` copies
the whole accumulated buffer every iteration, so emitting a `trun` for a
few-hundred-sample fragment turns quadratic. For `mdat`, collect the per-sample
slices into a list and `b"".join(...)` once — one allocation of exactly the right
size.

**The length-prefix problem, and the clean way out.** Every box is
`[size][fourcc][payload]`, and the size includes the header, so you cannot write
the size until the payload exists. Two workable shapes: build each payload
bottom-up and wrap it (`pack(">I", len(payload) + 8) + fourcc + payload`), or
write a zero placeholder into a `bytearray`, remember the position, and patch it
with `struct.pack_into` once you know. Bottom-up composes better for a tree this
deep; the patch approach avoids re-copying nested payloads. Pick one and be
consistent, because mixing them is how a `moov` ends up eight bytes short.

**`trun`'s `data_offset` is the one that bites.** It is signed, relative to the
start of the *`moof`*, and it must point at the first byte of `mdat`'s payload —
which means it depends on the size of the `moof` you have not finished writing.
Get it wrong and a player reads garbage with no error message. This is the single
most common fMP4 bug, so plan for it: either compute the `moof` size before
emitting it, or patch the field afterwards.

**Determinism is a caching contract, and Python has traps for it.** The SPEC
grades `build_init_segment` on returning byte-identical output across calls,
because V4's `ETag` and every cache in front of this server depend on it. So: no
`time.time()` in `mvhd`'s creation/modification fields (write zeros), no `id()`,
no `hash()` (string hashing is salted per process — `PYTHONHASHSEED` randomizes
it, so anything derived from set iteration order differs between *runs*, which
is the worst kind of bug to reproduce). Iterating a `dict` is ordered and safe;
iterating a `set` is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from .isobmff import Track

__all__ = [
    "SegmentEntry",
    "SegmentIndex",
    "build_init_segment",
    "build_media_segment",
    "plan_segments",
]


@dataclass(slots=True)
class SegmentEntry:
    """One planned segment: which samples it covers and its place on the timeline.

    Computed from the sample table without touching a single media byte, which is
    what makes it cheap enough to recompute per request while the built bytes are
    memoized separately.
    """

    index: int
    """0-based segment index — matches the URL `.../seg/{index}` and the manifest."""

    start_time: int
    """Decode time of this segment's first sample, in the track timescale.

    Becomes the fragment's `tfdt` `baseMediaDecodeTime`, and equals the summed
    durations of every prior segment. That equality is what "each segment decodes
    standalone" means in practice: the player can drop into segment 40 and still
    know where on the timeline it lands."""

    duration: int
    """Total decode duration of this segment, in the track timescale. Drives the
    manifest's `#EXTINF`."""

    samples: range
    """Which of `Track.samples` this segment covers, as a half-open `range`.

    A `range` rather than a start/count pair because it is already everything you
    want: `len(entry.samples)` is the sample count, `for i in entry.samples`
    iterates them, and `entry.samples.start` is the first index."""

    def seconds(self, timescale: int) -> float:
        """Segment duration in seconds, given the track timescale.

        Divide *once*, here, from the integer tick count. Accumulating seconds as
        floats across a long asset is how the SPEC's "no rounding drift" criterion
        fails: 3600 additions of a float that cannot represent 1/30 exactly will
        not sum to the track duration."""
        return self.duration / timescale


@dataclass(slots=True)
class SegmentIndex:
    """The full segmentation plan for one rendition: the ordered segment list."""

    segments: list[SegmentEntry] = field(default_factory=list[SegmentEntry])

    def target_duration(self, timescale: int) -> int:
        """The longest segment in seconds, rounded **up** — HLS `#EXT-X-TARGETDURATION`.

        `ceil`, not `round`: the tag is an upper bound the spec requires every
        segment to respect, and a player uses it to size its buffer. Rounding a
        6.4-second maximum down to 6 makes the playlist invalid.
        """
        return max((ceil(entry.seconds(timescale)) for entry in self.segments), default=0)

    def total_duration(self) -> int:
        """Summed segment durations, in the track timescale.

        Should equal `Track.duration` exactly — the segmentation partitions the
        samples, so anything else means a gap or an overlap."""
        return sum(entry.duration for entry in self.segments)


def plan_segments(track: Track, target_secs: float) -> SegmentIndex:
    """Group a track's samples into keyframe-aligned segments of ~`target_secs`.

    TODO(V2): the segmentation policy.

      * Walk `track.samples`. A new segment may only *begin* on a sync sample. So
        the real unit of work is the GOP: the run of samples from one sync sample
        up to (not including) the next.
      * Accumulate GOPs. When adding the next one would push the accumulated
        duration past `target_secs`, close the current segment and start the next
        at that keyframe. Never split a GOP to hit the target — the keyframe
        boundary always wins, so real segment lengths cluster around the target
        rather than landing on it.
      * Set each entry's `start_time` to the running decode time and its
        `duration` to the sum of its samples' durations. Consecutive segments must
        be gapless: `segments[n + 1].start_time == segments[n].start_time +
        segments[n].duration`. Deriving one from the other rather than summing
        twice makes that true by construction.
      * A track with no sync samples flagged is all-keyframe content, and V1
        already represents that as every `Sample.is_sync` being True — so this
        should need no special case. If you find yourself writing one, check V1
        first.

    Two edge cases worth deciding on deliberately rather than discovering: a track
    whose *first* sample is not a sync sample (the leading samples cannot start a
    segment — do they join the first one, or are they dropped?), and a single GOP
    longer than the target (it becomes one over-long segment, which is correct,
    and `target_duration()` will report it).

    Compare `target_secs` against durations in *ticks*, not seconds — convert the
    target once (`target_secs * track.timescale`) and stay in integers. See
    `SegmentEntry.seconds` on why.
    """
    del track, target_secs
    raise NotImplementedError("V2: group samples into keyframe-aligned segments of ~target_secs")


def build_init_segment(track: Track) -> bytes:
    """Build the CMAF **init segment** — `ftyp` + `moov`, codec setup, no samples.

    TODO(V2): emit the boxes.

      * `ftyp`: a major brand plus compatible brands announcing fragmented/CMAF
        (`iso6`, `cmfc`, `mp41`... — decide and document what you claim, because a
        player is entitled to believe it).
      * `moov` = `mvhd` + `trak`(`tkhd` + `mdia`[`mdhd` + `hdlr` +
        `minf`[... `stbl` with the `stsd`/codec box built from
        `track.codec.setup`, and *empty* sample tables]]) + `mvex`(`trex`).
      * The `mvex`/`trex` is the whole point: it declares "samples live in
        fragments, not here". Without it a player reads a `moov` with empty sample
        tables and concludes the track has no media.
      * Zero media samples in the init segment, and the empty `stts`/`stsc`/
        `stsz`/`stco` boxes still have to be present with a count of zero.

    The result must be **byte-for-byte identical** across calls for the same track
    — see the module docstring on the Python-specific ways that quietly fails.
    """
    del track
    raise NotImplementedError("V2: emit ftyp + moov (codec config, mvex/trex, no samples)")


def build_media_segment(source: memoryview, track: Track, entry: SegmentEntry) -> bytes:
    """Build one **media segment** (`styp` + `moof` + `mdat`) for `entry`.

    `source` is a view over the whole source file; `entry.samples` indexes
    `track.samples`, whose `offset`/`size` locate each sample's bytes inside it.

    TODO(V2): emit the fragment.

      * `styp`: segment type box, same brands as `ftyp`.
      * `moof` = `mfhd`(sequence number = `entry.index + 1`) + `traf`[`tfhd`(track
        id, default flags) + `tfdt`(`baseMediaDecodeTime = entry.start_time`) +
        `trun`(per-sample size, duration and composition offset, with
        `data_offset` pointing at the first byte of `mdat`'s payload)].
      * `mdat`: the coded bytes — `source[s.offset : s.offset + s.size]` for each
        sample in `entry.samples`, in order. Collect the slices and `join` them;
        see the module docstring.
      * `trun`'s flags word decides which per-sample fields are present, and the
        writer and the reader have to agree exactly. Omitting composition offsets
        for a stream that has them silently destroys B-frame ordering.

    Returns `bytes`, deliberately: this is the one place a copy is *wanted*. The
    result outlives the mapping it was cut from, and it is bounded by the segment,
    not by the asset — which is the SPEC's memory criterion. Copy only this
    segment's samples and nothing else, and that bound holds for a 4 GB movie.
    """
    del source, track, entry
    raise NotImplementedError("V2: emit styp + moof(mfhd/tfhd/tfdt/trun) + mdat for this segment")
