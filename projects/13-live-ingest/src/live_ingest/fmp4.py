"""V3 — Live fMP4 repackaging: rewrap H.264/AAC into CMAF, no re-encode.

Module: `src/live_ingest/fmp4.py`.

The codecs arriving over RTMP are already `<video>`-playable; the job is not to
transcode them but to **remux**: build a CMAF **init segment** (`ftyp` + `moov`
carrying the codec setup and zero samples) and a running series of
**fragments** (`moof` + `mdat`) on a monotonic MP4 timeline, cutting **parts**
(~200 ms) on demand and starting **segments** on IDR keyframes. H.264 arrives
over RTMP in AVCC framing — length-prefixed NALUs — which is exactly MP4's, so
`Sample.data` goes into `mdat` byte-for-byte.

This overlaps project 11's box writing (`vod_streaming/segment.py`,
`build_init_segment` / `build_media_segment`) — reuse the discipline. What is
new is doing it **live**: the timeline comes from RTMP message timestamps
(32-bit milliseconds, wrapping) rather than a finished sample table, and only
the current fragment's samples are ever held. `docs/02-live-fmp4-remuxing.md`
teaches the box layout and the timeline.

## Python details worth deciding up front

**Write boxes inside-out, or patch sizes.** Every box starts with its own
32-bit size, which you do not know until its children are written. Two clean
shapes: build each box as a function returning `bytes` (children first, then
`struct.pack(">I4s", 8 + len(body), b"moof") + body`), or write into one
`bytearray` and back-patch the size with `struct.pack_into`. The first is
easier to test box by box; the second allocates less. Measure before caring.

**`b"".join(parts)` beats `+=`.** Concatenating `mdat` sample bytes with `+=` on
`bytes` copies the growing buffer each time — quadratic in samples per part.
Collect the pieces in a list and join once.

**Byte-stability is a pure-function discipline.** `build_init` for a given
`CodecConfig` must return identical bytes every call — the `immutable` cache
header on `init.mp4` depends on it. No `time.time()` in `mvhd`, no dict
iteration over something unordered, nothing that is not a function of the config.

**Integers do not overflow — which is the trap.** Rust would have caught a
`tfdt` that exceeded 32 bits at the type level. Python's `int` just keeps
growing, and `struct.pack(">I", 2**32)` raises only when you finally write it.
`baseMediaDecodeTime` in version-1 `tfdt` is 64-bit; use it. And unwrapping
RTMP's 32-bit millisecond wrap is arithmetic you do yourself (`& 0xFFFFFFFF`
and a jump check), because nothing here will wrap for you.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .flv import AudioSpecificConfig, AvcDecoderConfig

__all__ = [
    "VIDEO_TIMESCALE",
    "CodecConfig",
    "Fragmenter",
    "Sample",
    "TrackKind",
    "build_init",
]

VIDEO_TIMESCALE = 90_000
"""Ticks per second for the video track. At 30 fps a frame is exactly 3,000
ticks; in milliseconds it is 33.33… — the timescale exists so durations are exact.
The audio track conventionally uses its sample rate (an AAC frame is then
exactly 1,024 ticks)."""


class TrackKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class CodecConfig:
    """Codec setup extracted from the sequence headers (V2), enough for an init segment.

    This is the *setup*, carried in `moov` — never per-frame media. Frozen and
    hashable-by-value, which is also what lets `build_init` be memoized on it.
    """

    video: AvcDecoderConfig
    audio: AudioSpecificConfig | None
    """`None` for a video-only publish (`ffmpeg ... -an`)."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One access unit — a coded video frame or an AAC frame — to place in a fragment."""

    track: TrackKind
    data: bytes
    """Length-prefixed NALUs (video) or one raw AAC frame (audio), verbatim."""
    dts: int
    """Decode time in the track's timescale: derived from the RTMP timestamp,
    rebased so the session starts near zero, unwrapped so it never runs backwards."""
    pts: int
    """Presentation time in the track's timescale: `dts` plus the composition offset."""
    duration: int
    """In the track's timescale."""
    keyframe: bool
    """An IDR frame. A segment may only *start* on one."""


def build_init(config: CodecConfig) -> bytes:
    """Build the CMAF init segment, `ftyp` + `moov`, from the codec config (V3).

    TODO(V3): `ftyp` (major brand `iso6`, compatible `cmfc`), then `moov` with a
    `mvhd`, a video `trak` (`tkhd` with width/height; `stsd` → `avc1` → `avcC`
    holding `config.video.record` verbatim), an audio `trak` when there is audio
    (`stsd` → `mp4a` → `esds` wrapping `config.audio.record`), and `mvex` with
    one `trex` per track. Every sample table is present and **empty** — the
    samples live in fragments. Byte-stable for a given config.
    """
    raise NotImplementedError(
        "V3: build the CMAF init segment (ftyp + moov with avcC/esds, no samples)"
    )


class Fragmenter:
    """A stateful live fragmenter.

    Samples are pushed as they arrive off the session; `cut_part` wraps what has
    accumulated into one `moof` + `mdat` — an LL-HLS part. The decode time of
    each track's next fragment is the running anchor written into that track's
    `tfdt`, and it must advance by exactly each fragment's duration for the
    whole session. Nothing else is held: the memory bound is `pending`.
    """

    def __init__(self, config: CodecConfig) -> None:
        self.config = config
        self._pending: list[Sample] = []

    def push(self, sample: Sample) -> None:
        """Buffer one access unit for the fragment currently forming."""
        self._pending.append(sample)

    @property
    def pending(self) -> Sequence[Sample]:
        """The samples since the last cut — the only media this object holds."""
        return self._pending

    def cut_part(self) -> bytes:
        """Wrap the pending samples into one `moof` + `mdat` fragment (V3).

        TODO(V3): `mfhd` (a sequence number that increments per fragment), then
        per track a `traf`: `tfhd` (track id, defaults), `tfdt` (version 1,
        `baseMediaDecodeTime` = that track's running anchor), and `trun` (sample
        count, data offset into `mdat`, and each sample's
        duration/size/flags/composition offset — sync-sample flags on
        keyframes). Then `mdat` with the sample bytes in `trun` order. Advance
        each track's anchor by its fragment duration and clear `pending`.

        Where the part/segment cut *decisions* live — here, or in the session
        that calls this — is yours; the returned bytes are built once and served
        to every viewer from the live window.
        """
        raise NotImplementedError(
            "V3: cut the buffered samples into one moof+mdat fragment (a part)"
        )
