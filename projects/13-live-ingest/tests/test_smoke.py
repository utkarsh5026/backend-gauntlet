"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V4; those are yours to
write, and the SPEC's "Proof" lines name what each one has to demonstrate. What
is here is the plumbing: both planes come up, the live window behaves, the
delivery routes answer with the right headers, a blocking reload really parks
and really wakes, a publisher connection really reaches V1 — and the unbuilt
parts raise.

That last group is the worklist made executable. When you implement a vertical,
its test there is the first thing that should fail — delete it then.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from live_ingest import amf, flv, fmp4, llhls
from live_ingest.config import Settings
from live_ingest.errors import BadRequestError
from live_ingest.ingest import describe_failure
from live_ingest.live import LiveEdge, LiveRegistry, LiveStream, Part, Segment, WindowSnapshot
from live_ingest.main import create_app
from live_ingest.routes import safe_key
from live_ingest.rtmp import HANDSHAKE_SIZE, RTMP_VERSION, ChunkStreamReader
from live_ingest.state import AppState


def part(data: bytes = b"moof+mdat", *, independent: bool = False) -> Part:
    return Part(data=data, duration=0.3, independent=independent)


async def eventually(condition: object, timeout: float = 5.0) -> None:
    """Poll a zero-arg callable until it is truthy, or fail after `timeout`."""
    assert callable(condition)
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_stream_keys_are_split_trimmed_and_deduplicated() -> None:
    settings = Settings(stream_keys=" a, b,,a ,c")
    assert settings.allowed_keys == frozenset({"a", "b", "c"})


def test_stream_keys_from_the_environment_are_comma_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason `stream_keys` is a `str`: a `list` field would demand JSON."""
    monkeypatch.setenv("STREAM_KEYS", "alpha,beta")
    assert Settings().allowed_keys == frozenset({"alpha", "beta"})


def test_a_segment_shorter_than_a_part_is_a_startup_error() -> None:
    with pytest.raises(ValidationError, match="TARGET_SEGMENT_SECS"):
        Settings(target_part_secs=1.0, target_segment_secs=0.5)


# --------------------------------------------------------------------------- #
# The delivery plane
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_nothing_is_on_air(client: httpx.AsyncClient) -> None:
    assert (await client.get("/live")).json() == {"live": []}


async def test_status_reports_the_bound_rtmp_port(
    client: httpx.AsyncClient, state: AppState
) -> None:
    body = (await client.get("/status")).json()
    assert body["rtmp_port"] == state.ingest.port > 0
    assert body["live_streams"] == 0
    assert body["last_session_failure"] is None
    assert body["open_ingest"] is False


async def test_metrics_export_the_labelled_series_at_zero(client: httpx.AsyncClient) -> None:
    body = (await client.get("/metrics")).text
    assert 'live_ingest_blocking_reloads_total{outcome="timed_out"} 0.0' in body
    assert 'live_ingest_viewer_requests_total{kind="part"} 0.0' in body
    assert "live_ingest_hold_seconds_bucket" in body


async def test_cors_lets_a_cross_origin_player_fetch(client: httpx.AsyncClient) -> None:
    response = await client.get("/live", headers={"Origin": "http://player.example"})
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    "path",
    [
        "/live/nobody/index.m3u8",
        "/live/nobody/init.mp4",
        "/live/nobody/seg/0.m4s",
        "/live/nobody/part/0/0.m4s",
    ],
)
async def test_a_stream_that_is_not_on_air_is_a_clean_404(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


@pytest.mark.parametrize("key", ["", ".", "..", "a\\b", "a\0b", "a/b"])
def test_unsafe_stream_keys_are_refused(key: str) -> None:
    with pytest.raises(BadRequestError):
        safe_key(key)


@pytest.mark.parametrize(
    "path",
    [
        "/live/a%5Cb/index.m3u8",  # backslash, decoded before routing
        "/live/a%00b/init.mp4",  # NUL
        "/live/testkey/seg/abc.m4s",
        "/live/testkey/part/0/-1.m4s",
        "/live/testkey/index.m3u8?_HLS_msn=-1",
    ],
)
async def test_malformed_requests_are_400_not_422(client: httpx.AsyncClient, path: str) -> None:
    """A flat JSON error, never FastAPI's 422 tree echoing the input back."""
    response = await client.get(path)
    assert response.status_code == 400
    assert set(response.json()) == {"error"}


async def test_a_part_is_served_short_lived_by_reference(
    client: httpx.AsyncClient, state: AppState
) -> None:
    data = b"\x00\x00\x00\x10moof" + bytes(8)
    stream = state.registry.open("testkey")
    stream.push_part(part(data, independent=True), start_segment=True)

    response = await client.get("/live/testkey/part/0/0.m4s")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "video/iso.segment"
    assert response.headers["cache-control"] == "public, max-age=5"
    assert (await client.get("/live")).json() == {"live": ["testkey"]}
    assert (await client.get("/live/testkey/part/0/1.m4s")).status_code == 404


async def test_init_is_503_until_built_then_immutable(
    client: httpx.AsyncClient, state: AppState
) -> None:
    stream = state.registry.open("testkey")
    not_ready = await client.get("/live/testkey/init.mp4")
    assert not_ready.status_code == 503
    assert not_ready.headers["retry-after"] == "1"

    stream.set_init(b"ftyp+moov")
    ready = await client.get("/live/testkey/init.mp4")
    assert ready.status_code == 200
    assert ready.headers["content-type"] == "video/mp4"
    assert "immutable" in ready.headers["cache-control"]


async def test_a_segment_is_404_while_forming_and_immutable_once_complete(
    client: httpx.AsyncClient, state: AppState
) -> None:
    stream = state.registry.open("testkey")
    stream.push_part(part(), start_segment=True)
    assert (await client.get("/live/testkey/seg/0.m4s")).status_code == 404

    stream.finish_segment(b"whole segment")
    response = await client.get("/live/testkey/seg/0.m4s")
    assert response.status_code == 200
    assert response.content == b"whole segment"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_a_blocking_reload_parks_until_its_part_exists(
    client: httpx.AsyncClient, state: AppState
) -> None:
    """The V4 mechanism, end to end through HTTP — up to the renderer.

    The request for part (0, 1) must *not* answer while only (0, 0) exists, and
    must wake the moment (0, 1) is pushed. It then reaches the unbuilt renderer,
    which is exactly where a scaffold should stop.
    """
    stream = state.registry.open("testkey")
    stream.push_part(part(independent=True), start_segment=True)

    held = asyncio.create_task(client.get("/live/testkey/index.m3u8?_HLS_msn=0&_HLS_part=1"))
    await asyncio.sleep(0.1)
    assert not held.done(), "a blocking reload answered before its part existed"

    stream.push_part(part(), start_segment=False)
    with pytest.raises(NotImplementedError, match="V4"):
        async with asyncio.timeout(2):
            await held


async def test_a_blocking_reload_is_bounded() -> None:
    """A target that never arrives is held for `max_block`, then answered."""
    stream = LiveStream("k", window_segments=3)
    started = time.monotonic()
    with pytest.raises(NotImplementedError, match="V4"):
        await llhls.media_playlist(
            stream, llhls.ReloadParams(msn=99, part=0), part_target=0.3, max_block=0.05
        )
    assert 0.05 <= time.monotonic() - started < 1.0


# --------------------------------------------------------------------------- #
# The live window — wired, so these assert behaviour, not todos
# --------------------------------------------------------------------------- #


def test_the_edge_is_ordered_msn_major_part_minor() -> None:
    assert LiveEdge(13, 0) > LiveEdge(12, 9)
    assert LiveEdge(12, 3) >= LiveEdge(12, 3)


def test_push_part_opens_segments_and_advances_the_edge() -> None:
    stream = LiveStream("k", window_segments=3)
    assert stream.edge is None
    assert stream.push_part(part(), start_segment=False) == LiveEdge(0, 0)
    assert stream.push_part(part(), start_segment=False) == LiveEdge(0, 1)
    assert stream.push_part(part(), start_segment=True) == LiveEdge(1, 0)


def test_the_window_is_bounded_and_an_msn_is_never_reused() -> None:
    stream = LiveStream("k", window_segments=3)
    for _ in range(5):
        stream.push_part(part(), start_segment=True)
    snapshot = stream.snapshot()
    assert [segment.msn for segment in snapshot.segments] == [2, 3, 4]
    assert stream.part_bytes(1, 0) is None, "an evicted segment must be gone"
    assert stream.push_part(part(), start_segment=True) == LiveEdge(5, 0)


def test_viewers_share_the_window_bytes_rather_than_copies() -> None:
    """The fan-out contract: N viewers, one `bytes` object."""
    data = b"built once"
    stream = LiveStream("k", window_segments=3)
    stream.push_part(part(data), start_segment=True)
    assert stream.part_bytes(0, 0) is data


def test_a_snapshot_does_not_change_under_the_renderer() -> None:
    stream = LiveStream("k", window_segments=3)
    stream.push_part(part(), start_segment=True)
    snapshot = stream.snapshot()
    stream.push_part(part(), start_segment=False)
    assert len(snapshot.segments[0].parts) == 1


async def test_await_edge_wakes_exactly_when_the_target_is_reached() -> None:
    stream = LiveStream("k", window_segments=3)
    waiter = asyncio.create_task(stream.await_edge(LiveEdge(0, 1)))

    stream.push_part(part(), start_segment=True)
    await asyncio.sleep(0.01)
    assert not waiter.done(), "woke on (0, 0) while waiting for (0, 1)"

    stream.push_part(part(), start_segment=False)
    async with asyncio.timeout(1):
        assert await waiter == LiveEdge(0, 1)


async def test_many_waiters_wake_on_one_push() -> None:
    stream = LiveStream("k", window_segments=3)
    waiters = [asyncio.create_task(stream.await_edge(LiveEdge(0, 0))) for _ in range(200)]
    await asyncio.sleep(0)
    stream.push_part(part(), start_segment=True)
    async with asyncio.timeout(1):
        assert set(await asyncio.gather(*waiters)) == {LiveEdge(0, 0)}


async def test_await_edge_returns_when_the_stream_ends() -> None:
    stream = LiveStream("k", window_segments=3)
    waiter = asyncio.create_task(stream.await_edge(LiveEdge(5, 0)))
    await asyncio.sleep(0)
    stream.mark_ended()
    async with asyncio.timeout(1):
        assert await waiter is None


def test_the_registry_authorizes_from_the_allow_list() -> None:
    assert LiveRegistry(Settings(stream_keys="a,b")).authorize("a")
    assert not LiveRegistry(Settings(stream_keys="a,b")).authorize("c")
    assert LiveRegistry(Settings(stream_keys="")).authorize("anything")


def test_the_registry_opens_lists_and_closes() -> None:
    registry = LiveRegistry(Settings(stream_keys=""))
    first = registry.open("b")
    assert registry.open("b") is first
    registry.open("a")
    assert registry.live_keys() == ["a", "b"]
    registry.close("b")
    assert registry.get("b") is None
    assert len(registry) == 1


# --------------------------------------------------------------------------- #
# The ingest plane: a real TCP listener, really reaching V1
# --------------------------------------------------------------------------- #


async def test_a_publisher_reaches_v1_and_the_server_keeps_accepting(
    client: httpx.AsyncClient, state: AppState
) -> None:
    """The whole scaffold, end to end, in the state it is meant to be in.

    Opens a real TCP connection at the RTMP port and sends C0+C1. The session
    reaches `rtmp.handshake`, which raises; that one connection closes, the
    failure is recorded naming the function, and the listener is untouched.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", state.ingest.port)
    writer.write(bytes([RTMP_VERSION]) + bytes(HANDSHAKE_SIZE))
    await writer.drain()

    await eventually(lambda: state.ingest.last_failure is not None)
    failure = state.ingest.last_failure
    assert failure is not None
    assert failure.startswith("NotImplementedError: V1")
    assert "handshake" in failure
    # The socket is closed. It may arrive as EOF *or* as a reset: the session
    # closed with our unread C1 bytes still in its receive buffer, and a kernel
    # answers that close with RST rather than FIN. Both mean "gone".
    try:
        assert await reader.read() == b""
    except ConnectionResetError:
        pass
    writer.close()

    body = (await client.get("/status")).json()
    assert body["last_session_failure"] == failure
    metrics = (await client.get("/metrics")).text
    assert 'live_ingest_rtmp_sessions_ended_total{reason="unimplemented"} 1.0' in metrics

    # One broadcaster's failure is not the server's.
    _, second = await asyncio.open_connection("127.0.0.1", state.ingest.port)
    second.close()


async def test_shutdown_stops_the_rtmp_listener(settings: Settings) -> None:
    application: FastAPI = create_app(settings)
    async with application.router.lifespan_context(application):
        app_state = application.state.app_state
        assert isinstance(app_state, AppState)
        port = app_state.ingest.port
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


def test_describe_failure_names_the_innermost_function() -> None:
    def the_todo() -> None:
        raise NotImplementedError("V9: something")

    try:
        the_todo()
    except NotImplementedError as exc:
        assert describe_failure(exc).startswith("NotImplementedError: V9: something (")
        assert describe_failure(exc).endswith(" the_todo)")


def test_set_chunk_size_is_clamped() -> None:
    """Wired: a publisher cannot announce a chunk larger than any message."""
    reader = ChunkStreamReader(max_message_size=1024)
    reader.set_chunk_size(0)
    assert reader.chunk_size == 1
    reader.set_chunk_size(0x7FFF_FFFF)
    assert reader.chunk_size == 1024


# --------------------------------------------------------------------------- #
# The worklist — each of these fails the day its vertical works. Delete it then.
# --------------------------------------------------------------------------- #


async def test_v1_chunk_reader_is_unbuilt() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(bytes([0x03]) + bytes(11))
    reader.feed_eof()
    with pytest.raises(NotImplementedError, match="V1"):
        await ChunkStreamReader().read_message(reader)


def test_v2_amf0_codec_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError, match="V2"):
        amf.decode(bytes([amf.Marker.NULL]))
    with pytest.raises(NotImplementedError, match="V2"):
        amf.encode("_result", 1.0, {"code": "NetConnection.Connect.Success"}, None)


def test_v2_flv_parsing_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError, match="V2"):
        flv.parse_video_tag(b"\x17\x00\x00\x00\x00")
    with pytest.raises(NotImplementedError, match="V2"):
        flv.parse_audio_tag(b"\xaf\x00")
    with pytest.raises(NotImplementedError, match="V2"):
        flv.parse_avc_decoder_config(b"\x01")
    with pytest.raises(NotImplementedError, match="V2"):
        flv.parse_audio_specific_config(b"\x12\x10")


def test_v3_packager_is_unbuilt() -> None:
    config = fmp4.CodecConfig(
        video=flv.AvcDecoderConfig(record=b"\x01", nal_length_size=4, width=320, height=240),
        audio=None,
    )
    with pytest.raises(NotImplementedError, match="V3"):
        fmp4.build_init(config)

    fragmenter = fmp4.Fragmenter(config)
    fragmenter.push(
        fmp4.Sample(
            track=fmp4.TrackKind.VIDEO, data=b"nalu", dts=0, pts=0, duration=3000, keyframe=True
        )
    )
    assert len(fragmenter.pending) == 1
    with pytest.raises(NotImplementedError, match="V3"):
        fragmenter.cut_part()


def test_v4_playlist_renderer_is_unbuilt() -> None:
    snapshot = WindowSnapshot(
        segments=(Segment(msn=0, program_date_time=datetime.now(UTC)),),
        ended=False,
        edge=LiveEdge(0, 0),
    )
    with pytest.raises(NotImplementedError, match="V4"):
        llhls.render_media_playlist(snapshot, part_target=0.3)
