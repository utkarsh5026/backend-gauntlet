"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V4; those are yours, and each
vertical's "Proof" line names what it has to demonstrate. What is here is the
plumbing: the app boots, every route is reachable, the framework's validation
answers, the wired parts of each plane behave, and the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
its tests here are the first thing that should fail — delete them then.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from live_platform.chat import BusEnvelope, ChatHub, ChatMessage, Subscription
from live_platform.config import DEFAULT_LADDER, Rendition, Settings, parse_ladder
from live_platform.control import StreamState
from live_platform.errors import (
    AppError,
    ConflictError,
    DependencyError,
    NotFoundError,
    RejectedError,
    UpstreamError,
    UpstreamTimeoutError,
)
from live_platform.metrics import TRANSITION_TARGETS
from live_platform.state import AppState
from live_platform.workers import TranscodeJob

# --------------------------------------------------------------------------- #
# The admin plane
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_readyz_is_green_with_no_background_tasks(client: httpx.AsyncClient) -> None:
    assert (await client.get("/readyz")).status_code == 200


async def test_status_reports_config_and_an_empty_platform(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    body = (await client.get("/status")).json()
    assert body["streams_live"] == 0
    assert body["streams"] == []
    assert [rung["name"] for rung in body["ladder"]] == [r.name for r in settings.ladder]
    assert body["part_secs"] == settings.part_secs
    assert body["transcode"] == {
        "queue_depth": 0,
        "max_replicas": settings.max_transcode_replicas,
    }
    assert body["chat"]["active_channels"] == 0
    assert body["edge_origin"] == settings.packager_origin.rstrip("/")
    assert body["background_errors"] == []


async def test_metrics_exposes_the_platform_series(client: httpx.AsyncClient) -> None:
    """Labelled children export at zero, not absent — see `metrics.preregister`."""
    body = (await client.get("/metrics")).text
    assert "live_streams 0.0" in body
    assert "live_transcode_queue_depth 0.0" in body
    assert 'live_edge_requests_total{outcome="coalesced"} 0.0' in body
    assert 'live_transcode_jobs_total{result="retried"} 0.0' in body
    assert "live_glass_to_glass_seconds_bucket" in body


async def test_every_enterable_state_has_a_transition_series(client: httpx.AsyncClient) -> None:
    """`metrics` holds its labels as literals; this keeps them in step with V1."""
    assert set(TRANSITION_TARGETS) == {s.value for s in StreamState} - {"offline"}
    body = (await client.get("/metrics")).text
    for to in TRANSITION_TARGETS:
        assert f'live_stream_transitions_total{{to="{to}"}} 0.0' in body


# --------------------------------------------------------------------------- #
# Every route is reachable, and says which vertical it waits on
# --------------------------------------------------------------------------- #


async def test_ingest_start_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/ingest/start", json={"stream_key": "demo", "ingest_node": "node-1"}
    )
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V1:")


async def test_ingest_stop_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.post("/ingest/stop", json={"stream_key": "demo"})
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V1:")


async def test_a_malformed_ingest_body_is_a_4xx(client: httpx.AsyncClient) -> None:
    """Already true, courtesy of the framework — Protocols → ingest interop."""
    assert (await client.post("/ingest/start", json={"ingest_node": "n"})).status_code == 422
    assert (await client.post("/ingest/start", content=b"not json")).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/live/demo/master.m3u8", id="master"),
        pytest.param("/live/demo/720p/index.m3u8", id="media"),
        pytest.param("/live/demo/720p/index.m3u8?_HLS_msn=3&_HLS_part=1", id="blocking_reload"),
        pytest.param("/live/demo/720p/seg-3.1.m4s", id="segment"),
    ],
)
async def test_playback_reaches_v3(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V3:")


async def test_a_ranged_segment_reaches_v3(client: httpx.AsyncClient) -> None:
    response = await client.get("/live/demo/720p/seg-3.m4s", headers={"Range": "bytes=0-99"})
    assert response.status_code == 501
    assert response.json()["todo"].startswith("V3:")


async def test_a_negative_media_sequence_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/live/demo/720p/index.m3u8?_HLS_msn=-1")).status_code == 422


# --------------------------------------------------------------------------- #
# Config — the type annotation is the parser
# --------------------------------------------------------------------------- #


def test_the_default_ladder_is_three_rungs() -> None:
    ladder = parse_ladder(DEFAULT_LADDER)
    assert [rung.name for rung in ladder] == ["1080p", "720p", "480p"]
    assert ladder[1] == Rendition(name="720p", width=1280, height=720, bitrate_kbps=3000)


def test_a_custom_ladder_parses() -> None:
    config = Settings(abr_ladder="source:1920x1080@8000, mobile:640x360@600")
    assert [(r.name, r.bitrate_kbps) for r in config.ladder] == [("source", 8000), ("mobile", 600)]


@pytest.mark.parametrize(
    "ladder",
    [
        pytest.param("720p:1280x720", id="missing_bitrate"),
        pytest.param("", id="empty"),
        pytest.param("720p:1280x720@3000,720p:1280x720@2000", id="duplicate_name"),
        pytest.param("../x:1x1@1", id="traversal_in_name"),
        pytest.param("720p:0x720@3000", id="zero_width"),
    ],
)
def test_a_bad_ladder_fails_at_startup(ladder: str) -> None:
    with pytest.raises(ValidationError):
        Settings(abr_ladder=ladder)


def test_a_part_longer_than_its_segment_fails_at_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(segment_secs=2.0, part_secs=2.0)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NotFoundError, 404),
        (ConflictError, 409),
        (RejectedError, 400),
        (UpstreamError, 502),
        (UpstreamTimeoutError, 504),
        (DependencyError, 503),
    ],
)
def test_each_error_maps_to_its_status(error: type[AppError], status: int) -> None:
    assert error.status_code == status


def test_an_origin_timeout_is_still_an_upstream_error() -> None:
    assert issubclass(UpstreamTimeoutError, UpstreamError)


def test_internal_detail_never_reaches_a_client() -> None:
    assert not UpstreamError.client_safe
    assert not DependencyError.client_safe


# --------------------------------------------------------------------------- #
# Wire types — serialization comes free from pydantic
# --------------------------------------------------------------------------- #


def test_a_transcode_job_round_trips() -> None:
    job = TranscodeJob(id="j1", stream_key="demo", rendition="720p", enqueued_at_ms=1)
    assert TranscodeJob.model_validate_json(job.model_dump_json()) == job


def test_a_bus_envelope_round_trips() -> None:
    message = ChatMessage(stream_key="demo", user="ada", body="POG", sent_at_ms=1)
    envelope = BusEnvelope(origin_node="node-a", message=message)
    assert BusEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


def test_two_subscribers_to_one_stream_are_two_subscribers() -> None:
    """Identity equality — see `Subscription`'s docstring."""
    first = Subscription("demo", asyncio.Queue[ChatMessage](maxsize=1))
    second = Subscription("demo", asyncio.Queue[ChatMessage](maxsize=1))
    assert len({first, second}) == 2


# --------------------------------------------------------------------------- #
# The worklist, made executable. Delete each as you implement its vertical.
# --------------------------------------------------------------------------- #


async def test_v1_control_plane_is_unwritten(state: AppState) -> None:
    platform = state.platform
    assert platform.snapshot() == []
    assert platform.live_count == 0
    with pytest.raises(NotImplementedError):
        await platform.reconcile()
    with pytest.raises(NotImplementedError):
        await platform.on_ingest_start("demo", "node-1")
    with pytest.raises(NotImplementedError):
        await platform.transition("demo", StreamState.LIVE)
    with pytest.raises(NotImplementedError):
        await platform.on_ingest_stop("demo")


async def test_v2_worker_pool_is_unwritten(state: AppState) -> None:
    workers = state.workers
    assert workers.queue_depth == 0
    job = TranscodeJob(id="j1", stream_key="demo", rendition="720p", enqueued_at_ms=1)
    with pytest.raises(NotImplementedError):
        await workers.ensure_queue()
    with pytest.raises(NotImplementedError):
        await workers.enqueue(job)
    with pytest.raises(NotImplementedError):
        await workers.claim("worker-1")
    with pytest.raises(NotImplementedError):
        await workers.complete("j1")
    with pytest.raises(NotImplementedError):
        workers.desired_replicas()


async def test_v3_edge_is_unwritten(state: AppState) -> None:
    from live_platform.edge import PlaylistCursor, parse_range_header

    edge = state.edge
    with pytest.raises(NotImplementedError):
        parse_range_header("bytes=0-99")
    with pytest.raises(NotImplementedError):
        await edge.master_playlist("demo")
    with pytest.raises(NotImplementedError):
        await edge.media_playlist("demo", "720p", PlaylistCursor(msn=3, part=1))
    with pytest.raises(NotImplementedError):
        await edge.segment("demo", "720p", "seg-3.m4s", None)


async def test_v4_chat_hub_is_unwritten(state: AppState) -> None:
    hub: ChatHub = state.chat
    assert hub.active_channels == 0
    assert hub.local_presence("demo") == 0
    with pytest.raises(NotImplementedError):
        hub.join("demo")
    with pytest.raises(NotImplementedError), hub.subscribe("demo"):
        pass
    message = ChatMessage(stream_key="demo", user="ada", body="hi", sent_at_ms=1)
    with pytest.raises(NotImplementedError):
        await hub.publish(message)
    with pytest.raises(NotImplementedError):
        await hub.run_bus()
