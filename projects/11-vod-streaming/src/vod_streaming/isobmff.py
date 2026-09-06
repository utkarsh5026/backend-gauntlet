"""V1 — The ISO-BMFF demuxer: read the MP4 container by hand.

Module: `src/vod_streaming/isobmff.py`.

An MP4 is a tree of length-prefixed **boxes** (a.k.a. atoms): each is
`[size uint32][type 4 chars][payload...]`, with a 64-bit size escape when
`size == 1` and "runs to end of file" when `size == 0`. The parts that matter
here:

    ftyp                                       brand / compatibility
    moov -> trak -> mdia -> minf -> stbl       per-track metadata, and inside
    `stbl` the sample tables:
        stsd         codec description (e.g. `avc1` -> `avcC` with SPS/PPS)
        stts         decode durations, run-length encoded (count x delta)
        ctts         composition offsets (pts - dts) for B-frame reordering
        stsc         sample-to-chunk mapping, also run-length encoded
        stsz         sample sizes
        stco / co64  chunk file offsets (32- or 64-bit)
        stss         sync-sample (keyframe) list — absent means *every* sample
                     is a keyframe
    mdat                                       the coded media bytes the tables
                                               point into

The whole job of V1 is to *cross-reference* those tables into one flat `Sample`
list per track: for every frame, its byte offset in the file, its size, its
decode time, its composition offset, and whether it is a keyframe. Everything
downstream — segmenting, manifests, delivery — reads that list and never touches
a box again.

## Three Python details that decide whether this works

**Slicing does not raise; that is the danger.** `data[pos : pos + 4]` on a
truncated buffer quietly hands back two bytes, and `int.from_bytes` cheerfully
turns those two bytes into a number. A malformed file therefore does not crash —
it produces *plausible wrong answers*, which is far worse, and it is why the SPEC
grades "a truncated box is rejected with an error". Check `pos + n <= end`
*before* every read, or use `struct.unpack_from`, which does raise
(`struct.error`) when the buffer is too small. Convert that to `MalformedMedia`.

**Big-endian is free.** `struct.unpack_from(">I", data, pos)` and
`int.from_bytes(data[a:b], "big")` are exactly the box wire format, so this needs
no dependency. `struct` is faster for single fields; `int.from_bytes` reads
better for the odd 24- and 48-bit fields ISO-BMFF sprinkles around.

**`memoryview` is the reason a 4 GB movie fits.** The catalog hands you a view
over an `mmap` of the source, not its bytes. Slicing a `memoryview` costs
nothing and copies nothing; calling `bytes()` on one copies. So slice freely
while parsing, and copy only the few hundred bytes of codec setup you actually
need to keep — which is precisely why `CodecConfig.setup` below is `bytes` and
not a view. A view retained past the end of the request would pin the whole
mapping (and `mmap.close()` would raise `BufferError` while it lives).

## A ceiling worth measuring

A one-hour 30 fps track is ~108,000 samples. In Rust that list was a contiguous
`Vec<Sample>` of packed structs. In Python, even with `slots=True`, each `Sample`
is a separate heap object, and the list is a list of pointers to them — call it
two orders of magnitude more memory and a lot of allocator traffic. `slots=True`
below is the cheap 40% you get for free. If the sample table turns out to be what
the profiler points at, the next move is columnar: parallel `array("Q", ...)` /
`array("I", ...)` buffers instead of one object per frame. Do not guess — measure
it with `memray` and record what you found in `docs/11-benchmarks.md`. That gap
is a finding, not a failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

from .errors import MalformedMedia

__all__ = [
    "Box",
    "CodecConfig",
    "MediaInfo",
    "Sample",
    "Track",
    "TrackKind",
    "demux",
    "iter_boxes",
]


@dataclass(slots=True)
class Sample:
    """One coded sample (frame) located in the source file.

    `slots=True` is deliberate — see the module docstring on why a per-frame
    object is the memory ceiling of this design.
    """

    offset: int
    """Absolute byte offset of this sample's data in the source file.

    Absolute, not relative to `mdat`, because that is what `build_media_segment`
    needs to slice with — and resolving it is the cross-referencing job: chunk
    offset from `stco`/`co64`, plus the running sum of earlier sample sizes
    within that chunk from `stsz`, with `stsc` saying which chunk you are in."""

    size: int
    """Size of the sample in bytes (`stsz`)."""

    decode_time: int
    """Decode timestamp in the track's timescale — the running sum of `stts`
    deltas, not a field that exists anywhere in the file."""

    duration: int
    """This sample's decode duration in the track's timescale (its `stts` delta)."""

    composition_offset: int = 0
    """`ctts`: presentation_time = decode_time + this.

    Zero when the stream is in decode order (no B-frames, or no `ctts` box). It
    can be *negative* in version 1 of `ctts` — signed 32-bit — which is a real
    trap, because reading it as unsigned turns a small negative offset into a
    number near 2**32 and throws presentation timing off by 27 hours."""

    is_sync: bool = True
    """True if this is a sync sample (keyframe / IDR) — a valid segment boundary.

    Defaults to True because that is what an *absent* `stss` box means: every
    sample is a random-access point. Only when `stss` is present does the
    distinction exist, and then only the listed samples are sync."""


class TrackKind(StrEnum):
    """What kind of media a track carries. Segmenting keys off the video track."""

    VIDEO = "video"
    AUDIO = "audio"
    OTHER = "other"


@dataclass(slots=True)
class CodecConfig:
    """Codec setup extracted from `stsd`, kept so V2 can build the init segment's
    `moov` without re-reading the source."""

    sample_entry: bytes
    """FourCC of the sample entry, e.g. `b"avc1"`, `b"hev1"`, `b"mp4a"`."""

    setup: bytes
    """The codec-specific configuration box payload — the `avcC` bytes (SPS/PPS)
    for H.264 — emitted verbatim into the init segment.

    `bytes`, i.e. a copy, not a `memoryview` slice: it is a few hundred bytes, it
    has to outlive the mapping it came from, and a retained view would pin the
    whole source file. See the module docstring."""

    width: int = 0
    """Coded picture width (0 for audio). Feeds the manifest's `RESOLUTION`."""

    height: int = 0


@dataclass(slots=True)
class Track:
    """One demuxed track: its timing base, kind, codec setup, and flat sample list."""

    id: int
    timescale: int
    """Ticks per second for every timing field on this track.

    Per *track*, not per file, and rarely a round number — 90000 and 24000 are
    both common. Every duration in this module is in these ticks; seconds appear
    only at the manifest boundary. Mixing the two up is the single most common
    way to produce a playlist whose durations are off by a factor of a thousand."""

    kind: TrackKind
    codec: CodecConfig
    samples: list[Sample] = field(default_factory=list[Sample])
    """Every sample in **decode** order. The heart of V1."""

    @property
    def duration(self) -> int:
        """Total decode duration of the track, in its own timescale."""
        return sum(sample.duration for sample in self.samples)

    def seconds(self, ticks: int) -> float:
        """Convert a duration in this track's timescale to seconds."""
        return ticks / self.timescale


@dataclass(slots=True)
class MediaInfo:
    """The whole demuxed asset.

    Owns no view into the source buffer, so it can outlive (and be cached
    independently of) the mapping it was parsed from.
    """

    tracks: list[Track] = field(default_factory=list[Track])

    def primary_video(self) -> Track:
        """The primary video track — what segments and manifests are built from.

        Muxing audio into its own rendition is a stretch goal; the graded path is
        video-first.
        """
        for track in self.tracks:
            if track.kind is TrackKind.VIDEO:
                return track
        raise MalformedMedia("source has no video track")


class Box(NamedTuple):
    """A parsed box header: its type and where its payload lives in the buffer."""

    fourcc: bytes
    """The 4-byte type, e.g. `b"moov"`. Compare against byte literals — decoding
    it to `str` would raise on the handful of boxes with non-ASCII types."""

    payload: slice
    """Byte range of the payload (after the header) within the source buffer, so
    `data[box.payload]` is the box's contents. A `slice` rather than a pair of
    ints because it composes: recursing into a container box is
    `iter_boxes(data, box.payload.start, box.payload.stop)`."""

    end: int
    """Offset just past the whole box — where the next sibling starts."""


def iter_boxes(data: memoryview, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Walk the boxes laid out between `start` and `end`, yielding each header.

    The Python re-aiming of what would be a cursor and a `read_box_header` call
    in a language without generators: containers nest, so the natural shape is
    `for box in iter_boxes(...)`, recursing into `box.payload` when the fourcc is
    one you care about.

    TODO(V1): implement the walk, including the length rules and the bounds
    checks. This is where a malformed file has to become an error:

      * `size >= 8`: the header itself is 8 bytes, so anything smaller is corrupt
        and — critically — a `size` of 0..7 that you advance by will loop forever.
      * `size == 1`: a 64-bit `largesize` follows the fourcc; the header is 16
        bytes and the payload starts after it.
      * `size == 0`: this box runs to `end`. Legal only as the last box.
      * `start + size <= end` for every box, checked *before* you slice. See the
        module docstring on why a short slice is more dangerous than an exception.

    Note that this raises immediately today because its body contains no `yield`
    — it is a plain function returning an iterator type. The moment you write
    your first `yield`, it becomes a generator, and nothing in the body runs until
    something iterates it. That is a genuine behaviour change worth knowing about:
    an exception raised inside a generator surfaces at the `for` loop, not at the
    call, so a bounds violation will be reported from a different line than you
    expect.
    """
    del data, start, end
    raise NotImplementedError("V1: walk the box tree with 32/64-bit sizes + bounds checks")


def demux(data: memoryview) -> MediaInfo:
    """Parse a source MP4 into per-track sample tables — the entire V1 deliverable.

    `data` is a view over the whole source file (an `mmap`, in the wired path).
    Slice it; do not copy it.

    TODO(V1): build the `MediaInfo`:

      1. Walk the top level for `moov`. Skip `ftyp`/`free`/`mdat` *payloads* —
         `mdat` is the media and can be gigabytes, and you never need to read it
         here, only to know that the offsets in `stco` point into the file at
         large.
      2. For each `trak` under `moov`, descend `mdia/minf/stbl` and read the
         sample tables. `stts`, `ctts` and `stsc` are run-length encoded — expand
         them. Watch the shape difference: `stts`/`ctts` are (count, value) runs
         you can flatten directly, while `stsc` runs are sparse and describe
         "chunks from here until the next entry hold N samples", so the last run
         extends to the end of the chunk list.
      3. Resolve each sample's absolute file offset: `stsc` says which chunk a
         sample belongs to, `stco`/`co64` gives that chunk's file offset, and the
         running sum of earlier `stsz` sizes *within that chunk* gives the rest.
      4. Mark sync samples from `stss` — a 1-based list of sample numbers. If
         `stss` is absent, every sample is a sync sample (already the `Sample`
         default).
      5. Pull `timescale` from `mdhd`, the kind from `hdlr`'s handler type
         (`vide`/`soun`), and the codec setup plus width/height from `stsd` and
         `tkhd`. Copy the setup bytes out — see `CodecConfig.setup`.

    Every table is a *full box*: 4 bytes of version+flags before the payload
    proper, and the version selects the field widths in `mdhd` and `ctts`. Reading
    a version-1 `mdhd` as version 0 shifts every field by 8 bytes and yields a
    timescale in the millions.

    Bounds: validate before slicing, always. `MalformedMedia` on anything that
    does not add up. That robustness is a graded criterion, and the property test
    the SPEC asks for will hunt for exactly the case you skipped.
    """
    del data
    raise NotImplementedError("V1: walk the ISO-BMFF box tree and build per-track sample tables")
