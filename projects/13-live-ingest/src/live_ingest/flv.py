"""V2 (part 3) — FLV media tags and the codec setup inside them.

Module: `src/live_ingest/flv.py`.

Once a session is publishing, every audio (type 8) and video (type 9) RTMP
message payload is an **FLV tag body**. The first ones of each kind are not
frames — they are the **sequence headers** carrying codec setup, and V2's fourth
Done-when box is extracting them:

    video tag:  [frame type 4b | codec id 4b] [AVCPacketType 1] [CompositionTime 3, signed] [body]
                 1 = keyframe     7 = AVC      0 = sequence header → body is the avcC
                                               1 = NALUs           → body is a frame

    audio tag:  [format 4b | rate 2b | size 1b | type 1b] [AACPacketType 1] [body]
                 10 = AAC                                   0 = sequence header → ASC
                                                            1 = raw             → a frame

The sequence headers are the *setup*: the `AVCDecoderConfigurationRecord`
(`avcC` — SPS/PPS, and the NAL length size) and the AAC `AudioSpecificConfig`
(profile, sample rate, channels). They arrive once, first, and they are
exactly what V3's init segment is made of. Everything after is per-frame media.
`docs/01-amf0-and-the-publish-state-machine.md` §5 has the table.

## Three details that bite

**CompositionTime is a *signed* 24-bit integer.** B-frames present *before*
they decode, so the offset can be negative. `int.from_bytes(b, "big",
signed=True)` handles it; reading it unsigned turns −33 ms into ~4.6 hours.

**Width and height are not in the avcC header — they are inside the SPS.** The
SPS is an exp-Golomb bitstream (variable-length integers, bit by bit), with
emulation-prevention bytes (`00 00 03`) that must be removed before you read
it. That bit reader is the single fiddliest parse in the project. A `bytes`
object plus a bit offset is enough; `int.from_bytes` over the whole SPS and
shifting is a clean alternative for something this small.

**The payload is already in memory, so nothing checks bounds for you.** Unlike
the socket, where `readexactly` refuses to return short, `payload[1:4]` on a
two-byte tag returns one byte. Check lengths first; raise `TruncatedError` /
`MalformedError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "AUDIO_FORMAT_AAC",
    "VIDEO_CODEC_AVC",
    "AacPacketType",
    "AudioSpecificConfig",
    "AudioTag",
    "AvcDecoderConfig",
    "AvcPacketType",
    "VideoTag",
    "parse_audio_specific_config",
    "parse_audio_tag",
    "parse_avc_decoder_config",
    "parse_video_tag",
]

VIDEO_CODEC_AVC = 7
"""FLV video codec id for H.264. Enhanced RTMP (HEVC/AV1 via FourCC) is a
From-the-field item, not V2."""

AUDIO_FORMAT_AAC = 10
"""FLV sound format for AAC."""


class AvcPacketType(IntEnum):
    SEQUENCE_HEADER = 0
    NALU = 1
    END_OF_SEQUENCE = 2


class AacPacketType(IntEnum):
    SEQUENCE_HEADER = 0
    RAW = 1


@dataclass(frozen=True, slots=True)
class VideoTag:
    keyframe: bool
    codec_id: int
    packet_type: int
    """Compare against `AvcPacketType`."""
    composition_time: int
    """PTS − DTS in milliseconds. Signed."""
    body: bytes


@dataclass(frozen=True, slots=True)
class AudioTag:
    sound_format: int
    packet_type: int
    """Compare against `AacPacketType`."""
    body: bytes


@dataclass(frozen=True, slots=True)
class AvcDecoderConfig:
    """The H.264 setup V3's video `trak` needs."""

    record: bytes
    """The whole `AVCDecoderConfigurationRecord` — the `avcC` box payload, verbatim."""
    nal_length_size: int
    """Bytes per NALU length prefix (`lengthSizeMinusOne + 1`), usually 4."""
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AudioSpecificConfig:
    """The AAC setup V3's audio `trak` needs."""

    record: bytes
    """The `AudioSpecificConfig` bytes, verbatim — they go inside `esds`."""
    object_type: int
    sample_rate: int
    channels: int


def parse_video_tag(payload: bytes) -> VideoTag:
    """Split an RTMP video message payload into its FLV tag fields (V2).

    TODO(V2): the first byte's two nibbles, then (for AVC) the packet type and
    the signed 24-bit composition time. Raise on a payload too short for the
    header its codec id implies.
    """
    raise NotImplementedError("V2: parse the FLV video tag header")


def parse_audio_tag(payload: bytes) -> AudioTag:
    """Split an RTMP audio message payload into its FLV tag fields (V2).

    TODO(V2): the flags byte, then (for AAC) the packet type.
    """
    raise NotImplementedError("V2: parse the FLV audio tag header")


def parse_avc_decoder_config(record: bytes) -> AvcDecoderConfig:
    """Read the avcC from an AVC sequence header body (V2).

    TODO(V2): version, profile, compatibility, level, `lengthSizeMinusOne`,
    then the SPS and PPS counts and length-prefixed units. Width and height
    come from decoding the first SPS — see the module docstring.
    """
    raise NotImplementedError("V2: parse the AVCDecoderConfigurationRecord (avcC + SPS size)")


def parse_audio_specific_config(record: bytes) -> AudioSpecificConfig:
    """Read the AAC AudioSpecificConfig from an AAC sequence header body (V2).

    TODO(V2): 5 bits object type, 4 bits sampling-frequency index (15 means an
    explicit 24-bit rate follows), 4 bits channel configuration — a bit reader
    again, on two bytes this time.
    """
    raise NotImplementedError("V2: parse the AAC AudioSpecificConfig")
