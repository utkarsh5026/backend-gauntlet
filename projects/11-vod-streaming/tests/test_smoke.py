"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4; the SPEC's "Proof" lines
say what those have to demonstrate and writing them is part of the work. What is
here is the plumbing: the app boots, the library scans, the server's own
endpoints answer, names are guarded, and the pure timing helpers do their
arithmetic.

The last group is the worklist made executable. Each one asserts that a vertical
still raises. When you implement one, its pin is the first thing that should fail
— delete it then, and replace it with the acceptance test the SPEC asks for.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from vod_streaming.catalog import Catalog
from vod_streaming.config import Settings
from vod_streaming.delivery import resolve_range, serve_ranged, text_response
from vod_streaming.errors import InvalidRequest, MalformedMedia, RangeNotSatisfiable
from vod_streaming.isobmff import (
    CodecConfig,
    MediaInfo,
    Sample,
    Track,
    TrackKind,
    demux,
    iter_boxes,
)
from vod_streaming.manifest import RenditionInfo, dash_mpd, hls_master_playlist, hls_media_playlist
from vod_streaming.routes import HLS_PLAYLIST, guard_name
from vod_streaming.segment import (
    SegmentEntry,
    SegmentIndex,
    build_init_segment,
    build_media_segment,
    plan_segments,
)

# --- wiring -------------------------------------------------------------------


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    """An inbound id survives the hop — it is what correlates a player's failed
    segment fetch with the cut that produced it in the log."""
    response = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


# --- the library scan ---------------------------------------------------------


async def test_assets_lists_the_scanned_library(client: httpx.AsyncClient) -> None:
    """Both rungs show up, sorted; the stray `.txt` and the empty title do not."""
    response = await client.get("/assets")
    assert response.status_code == 200
    assert response.json() == {"assets": [{"asset": "bbb", "renditions": ["1080p", "720p"]}]}


def test_a_missing_media_dir_is_created_not_fatal(tmp_path: Path) -> None:
    """First run has no media directory. Starting anyway beats a crash telling
    you to mkdir."""
    root = tmp_path / "not-yet"
    catalog = Catalog.load(root, 6.0)
    assert root.is_dir()
    assert catalog.asset_names() == []


def test_unknown_asset_has_no_renditions(media_dir: Path) -> None:
    """The lookup is a dict miss, not a filesystem probe — which is the whole
    traversal defence. See catalog.py's module docstring."""
    catalog = Catalog.load(media_dir, 6.0)
    assert catalog.rendition_ids("bbb") == ["1080p", "720p"]
    assert catalog.rendition_ids("../../etc") is None


# --- config -------------------------------------------------------------------


def test_media_dir_becomes_absolute(tmp_path: Path) -> None:
    """Resolved once at startup so every later containment question has a fixed
    thing to compare against."""
    assert Settings(media_dir=Path("./media")).media_dir.is_absolute()


@pytest.mark.parametrize("bad", [0, -1.5])
def test_target_segment_secs_must_be_positive(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(target_segment_secs=bad)


# --- name guarding ------------------------------------------------------------


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a\0b"])
def test_guard_name_rejects_filesystem_shapes(name: str) -> None:
    with pytest.raises(InvalidRequest):
        guard_name(name)


@pytest.mark.parametrize("name", ["bbb", "720p", "big-buck-bunny_2024"])
def test_guard_name_allows_ordinary_names(name: str) -> None:
    assert guard_name(name) == name


# --- the timing arithmetic that is already yours ------------------------------


def test_segment_seconds_divides_once_from_ticks() -> None:
    """Per-segment, from the integer tick count — never accumulated as floats.
    That is what keeps the summed EXTINFs from drifting off the track duration."""
    entry = SegmentEntry(index=0, start_time=0, duration=540_000, samples=range(0, 180))
    assert entry.seconds(90_000) == 6.0


def test_target_duration_rounds_up() -> None:
    """`#EXT-X-TARGETDURATION` is an upper bound the spec requires every segment
    to respect, so a 6.4-second maximum must report 7, not 6."""
    index = SegmentIndex(
        segments=[
            SegmentEntry(index=0, start_time=0, duration=540_000, samples=range(0, 180)),
            SegmentEntry(index=1, start_time=540_000, duration=576_000, samples=range(180, 372)),
        ]
    )
    assert index.target_duration(90_000) == 7
    assert index.total_duration() == 1_116_000


def test_target_duration_of_an_empty_plan_is_zero() -> None:
    assert SegmentIndex().target_duration(90_000) == 0


def test_track_duration_sums_its_samples() -> None:
    track = _track([_sample(0, 100, 0), _sample(100, 120, 3000), _sample(220, 90, 6000)])
    assert track.duration == 9000
    assert track.seconds(track.duration) == 0.1


def test_primary_video_rejects_an_audio_only_source() -> None:
    """A 5xx, not a 4xx: the file is the server's own. See errors.py."""
    audio = _track([], kind=TrackKind.AUDIO)
    with pytest.raises(MalformedMedia):
        MediaInfo(tracks=[audio]).primary_video()


def test_samples_default_to_sync() -> None:
    """Which is what an absent `stss` box means: every sample is a random-access
    point. Only when `stss` is present does the distinction exist."""
    assert _sample(0, 100, 0).is_sync


# --- plumbing that must work now ----------------------------------------------


def test_text_response_carries_the_playlist_content_type() -> None:
    response = text_response("#EXTM3U\n", HLS_PLAYLIST)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(HLS_PLAYLIST)
    assert response.headers["cache-control"] == "public, max-age=6"


def test_range_not_satisfiable_carries_the_required_header() -> None:
    """RFC 9110 requires a 416 to answer with the real length so a client that
    guessed wrong can retry. That envelope is plumbing; deciding a range is
    unsatisfiable is V4."""
    error = RangeNotSatisfiable(total=1000)
    assert error.status_code == 416
    assert error.headers() == {"content-range": "bytes */1000"}


# --- the scaffold's worklist, pinned -----------------------------------------


def test_the_demuxer_is_still_a_todo() -> None:
    """Delete once V1 lands."""
    data = memoryview(b"\x00\x00\x00\x08ftyp")
    with pytest.raises(NotImplementedError):
        demux(data)
    with pytest.raises(NotImplementedError):
        iter_boxes(data)


def test_the_segmenter_is_still_a_todo() -> None:
    """Delete once V2 lands."""
    track = _track([_sample(0, 100, 0)])
    entry = SegmentEntry(index=0, start_time=0, duration=3000, samples=range(0, 1))
    with pytest.raises(NotImplementedError):
        plan_segments(track, 6.0)
    with pytest.raises(NotImplementedError):
        build_init_segment(track)
    with pytest.raises(NotImplementedError):
        build_media_segment(memoryview(b""), track, entry)


def test_manifest_rendering_is_still_a_todo() -> None:
    """Delete once V3 lands."""
    track = _track([])
    with pytest.raises(NotImplementedError):
        hls_media_playlist(SegmentIndex(), track)
    with pytest.raises(NotImplementedError):
        hls_master_playlist([RenditionInfo(id="720p", bandwidth=0, uri="720p/index.m3u8")])
    with pytest.raises(NotImplementedError):
        dash_mpd(SegmentIndex(), track)


def test_range_delivery_is_still_a_todo() -> None:
    """Delete once V4 lands."""
    with pytest.raises(NotImplementedError):
        resolve_range("bytes=0-99", 1000)
    with pytest.raises(NotImplementedError):
        serve_ranged(b"x" * 1000, "video/iso.segment", None)


async def test_the_master_playlist_route_reaches_v3(client: httpx.AsyncClient) -> None:
    """The wiring is sound all the way to the vertical: the route resolves, the
    asset is found, and only then does it hit the unwritten part."""
    with pytest.raises(NotImplementedError):
        await client.get("/vod/bbb/master.m3u8")


async def test_a_media_request_reaches_v1(client: httpx.AsyncClient) -> None:
    """Anything needing media walks catalog -> mmap -> demux, so it lands on V1
    first — which is why V1 is the suggested place to start."""
    with pytest.raises(NotImplementedError):
        await client.get("/vod/bbb/720p/index.m3u8")
    with pytest.raises(NotImplementedError):
        await client.get("/vod/bbb/720p/seg/0")


async def test_an_unknown_asset_is_a_clean_404(client: httpx.AsyncClient) -> None:
    """Already true, and it must stay true: the lookup fails before any vertical
    runs, so this is a 404 rather than a NotImplementedError."""
    response = await client.get("/vod/nope/master.m3u8")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown asset"}


async def test_a_bad_segment_index_never_reaches_the_packager(client: httpx.AsyncClient) -> None:
    """Rejected by the route's own validation — see routes.py on 422 vs 400."""
    response = await client.get("/vod/bbb/720p/seg/-1")
    assert response.status_code == 422


# --- helpers ------------------------------------------------------------------


def _sample(offset: int, size: int, decode_time: int, duration: int = 3000) -> Sample:
    return Sample(offset=offset, size=size, decode_time=decode_time, duration=duration)


def _track(samples: list[Sample], kind: TrackKind = TrackKind.VIDEO) -> Track:
    return Track(
        id=1,
        timescale=90_000,
        kind=kind,
        codec=CodecConfig(sample_entry=b"avc1", setup=b"", width=1280, height=720),
        samples=samples,
    )
