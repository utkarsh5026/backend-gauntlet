"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4; the SPEC's "Proof"
lines say what those must demonstrate, and a `/quest` writes them. What is here
is the plumbing: the app boots, both UDP planes come up, the HTTP surface
validates hostile input, the cluster client really reaches the cluster routes,
config fails fast, and the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
its tests here are the first thing that should fail — delete them then.
"""

from __future__ import annotations

import asyncio
import socket as socketlib
from ipaddress import IPv6Address
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from pydantic import ValidationError

from global_conferencing.cascade import CascadeConfig, CascadeMesh
from global_conferencing.cluster import ClusterClient
from global_conferencing.config import PeerNode, Settings, parse_peers
from global_conferencing.errors import PeerUnreachableError
from global_conferencing.placement import Placement, PlacementConfig, Role, RoomPlacement
from global_conferencing.recording import Recorder, RecordingConfig
from global_conferencing.routes import MAX_LAYERS
from global_conferencing.routing import LayerRouter, RoutingConfig
from global_conferencing.rpc import (
    MAX_ENTRIES_PER_RPC,
    PlaceRoom,
    RegionInterest,
    ReplicateRequest,
    VoteRequest,
)
from global_conferencing.state import AppState
from global_conferencing.transport import MAX_DATAGRAM

LAYERS = [
    {"rid": "q", "ssrc": 111, "bitrate_bps": 150_000},
    {"rid": "h", "ssrc": 222, "bitrate_bps": 500_000},
    {"rid": "f", "ssrc": 333, "bitrate_bps": 2_000_000},
]


def send_udp(port: int, payload: bytes, count: int = 1) -> None:
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        for _ in range(count):
            sender.sendto(payload, ("127.0.0.1", port))


def peer(region: str, index: int = 0) -> PeerNode:
    return PeerNode(region, f"http://{region}.test", ("127.0.0.1", 7100 + index))


# --------------------------------------------------------------------------- #
# The admin plane
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_readyz_is_green_while_the_backbone_pump_runs(client: httpx.AsyncClient) -> None:
    assert (await client.get("/readyz")).status_code == 200


async def test_metrics_export_the_cascade_series_at_zero(client: httpx.AsyncClient) -> None:
    """Preregistered children export as zero rather than being absent."""
    body = (await client.get("/metrics")).text
    assert 'conf_relay_dropped_total{reason="loop"} 0.0' in body
    assert 'conf_datagrams_dropped_total{plane="cascade",reason="inbox_full"} 0.0' in body
    assert 'conf_placement_commits_total{kind="place_room"} 0.0' in body
    assert "conf_forward_seconds_bucket" in body


async def test_status_reports_identity_and_an_empty_mesh(
    client: httpx.AsyncClient,
    state: AppState,
    settings: Settings,
) -> None:
    body = (await client.get("/status")).json()
    assert body["node"]["region"] == "eu-west"
    assert body["node"]["peers"] == []
    # The *bound* port, not the configured 0.
    assert body["node"]["media_addr"] == f"127.0.0.1:{state.media.local_addr[1]}"
    assert body["placement"] == {
        "role": "follower",
        "term": 0,
        "leader": None,
        "max_rooms": settings.max_rooms,
        "quorum": 1,
        "rooms": [],
    }
    assert body["cascade"] == {"max_legs": settings.max_relay_links, "legs": []}
    assert body["routing"]["legs"] == []
    assert body["recording"]["active"] == []
    assert body["planes"]["backbone_pump_running"] is True
    assert body["planes"]["election_running"] is None


# --------------------------------------------------------------------------- #
# Signaling
# --------------------------------------------------------------------------- #


async def test_rooms_matches_the_web_playground_contract(client: httpx.AsyncClient) -> None:
    """`web/src/api.ts` reads exactly these keys as `GlobalTopology`."""
    body = (await client.get("/rooms")).json()
    assert body == {"region": "eu-west", "rooms": [], "relay_legs": []}


async def test_first_publish_reaches_v1_placement(client: httpx.AsyncClient) -> None:
    response = await client.post("/rooms/all-hands/publish", json={"layers": LAYERS})
    assert response.status_code == 501
    assert "V1" in response.json()["todo"]


async def test_subscribe_reaches_v1_membership(client: httpx.AsyncClient) -> None:
    response = await client.post("/rooms/all-hands/subscribe", json={"publisher": 1})
    assert response.status_code == 501
    assert "V1" in response.json()["todo"]


@pytest.mark.parametrize(
    "layers",
    [
        pytest.param([], id="no_layers"),
        pytest.param([{"rid": "q", "ssrc": 2**32, "bitrate_bps": 1}], id="ssrc_overflows_u32"),
        pytest.param([{"rid": "q", "ssrc": -1, "bitrate_bps": 1}], id="negative_ssrc"),
        pytest.param([{"rid": "q", "ssrc": 1, "bitrate_bps": 0}], id="zero_bitrate"),
        pytest.param(
            [{"rid": "q", "ssrc": n, "bitrate_bps": 1} for n in range(MAX_LAYERS + 1)],
            id="too_many_layers",
        ),
    ],
)
async def test_publish_rejects_hostile_bodies(
    client: httpx.AsyncClient,
    layers: list[dict[str, object]],
) -> None:
    """Validation runs in the model, before any vertical is reached."""
    response = await client.post("/rooms/demo/publish", json={"layers": layers})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "room",
    [
        pytest.param("x" * 65, id="too_long"),
        pytest.param(".hidden", id="leading_dot"),
        pytest.param("-dash", id="leading_dash"),
        pytest.param("bad room", id="space"),
    ],
)
async def test_room_ids_are_bounded(client: httpx.AsyncClient, room: str) -> None:
    """A room id becomes a log entry and (V4) a directory name — see `rpc.ROOM_ID_PATTERN`."""
    response = await client.post(f"/rooms/{room}/publish", json={"layers": LAYERS})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Cluster control
# --------------------------------------------------------------------------- #


async def test_cluster_vote_reaches_v1(client: httpx.AsyncClient) -> None:
    response = await client.post("/cluster/vote", json={"candidate_id": "n2", "term": 1})
    assert response.status_code == 501
    assert "V1" in response.json()["todo"]


async def test_cluster_replicate_reaches_v1(client: httpx.AsyncClient) -> None:
    entries = [
        {"kind": "place_room", "room_id": "all-hands", "region": "eu-west"},
        {"kind": "region_interest", "room_id": "all-hands", "region": "us-east", "joined": True},
    ]
    response = await client.post(
        "/cluster/replicate",
        json={"leader_id": "n2", "term": 1, "entries": entries},
    )
    assert response.status_code == 501


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"leader_id": "n2", "term": 1, "entries": [{"kind": "drop_table", "room_id": "a"}]},
            id="unknown_entry_kind",
        ),
        pytest.param(
            {
                "leader_id": "n2",
                "term": 1,
                "entries": [
                    {"kind": "place_room", "room_id": "a", "region": "r"}
                    for _ in range(MAX_ENTRIES_PER_RPC + 1)
                ],
            },
            id="too_many_entries",
        ),
        pytest.param({"leader_id": "n2", "term": -1}, id="negative_term"),
        pytest.param(
            {
                "leader_id": "n2",
                "term": 1,
                "entries": [{"kind": "place_room", "room_id": "..", "region": "r"}],
            },
            id="traversal_room_id",
        ),
    ],
)
async def test_replicate_rejects_hostile_bodies(
    client: httpx.AsyncClient,
    body: dict[str, object],
) -> None:
    assert (await client.post("/cluster/replicate", json=body)).status_code == 422


async def test_the_cluster_client_reaches_the_cluster_routes(app: FastAPI) -> None:
    """The sending half and the receiving half agree on paths and bodies.

    Points a `ClusterClient` at a second SFU through `ASGITransport`. The peer
    answers 501 (its V1 is unbuilt) — not 404 or 422, which is the proof — and
    the client folds that into the one error type consensus code catches.
    """
    cluster = ClusterClient(
        [peer("us-east")],
        timeout=1.0,
        transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(PeerUnreachableError) as caught:
            await cluster.request_vote("us-east", VoteRequest(candidate_id="n1", term=1))
        assert "501" in caught.value.detail
        with pytest.raises(PeerUnreachableError) as caught:
            await cluster.replicate("us-east", ReplicateRequest(leader_id="n1", term=1))
        assert "501" in caught.value.detail
    finally:
        await cluster.aclose()


async def test_an_unreachable_or_unknown_peer_is_one_error_type() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    cluster = ClusterClient([peer("us-east")], timeout=1.0, transport=httpx.MockTransport(refuse))
    try:
        with pytest.raises(PeerUnreachableError):
            await cluster.request_vote("us-east", VoteRequest(candidate_id="n1", term=1))
        with pytest.raises(PeerUnreachableError):
            await cluster.request_vote("mars-1", VoteRequest(candidate_id="n1", term=1))
    finally:
        await cluster.aclose()


# --------------------------------------------------------------------------- #
# The backbone: the socket is real, and the pump is really on it
# --------------------------------------------------------------------------- #


async def test_a_relay_datagram_reaches_v2_and_readyz_notices(
    state: AppState,
    client: httpx.AsyncClient,
) -> None:
    """The whole backbone path, end to end, in the state it is meant to be in.

    A datagram at the real cascade port is delivered by the datagram endpoint,
    the pump hands it to `CascadeMesh.on_relayed`, V2 raises, the pump dies, the
    HTTP server does not, and `/readyz` is what notices.
    """
    send_udp(state.backbone.local_addr[1], b"relay copy")
    async with asyncio.timeout(5):
        while not state.backbone_pump.done():
            await asyncio.sleep(0.01)

    assert isinstance(state.backbone_pump.exception(), NotImplementedError)
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 503
    assert (await client.get("/status")).json()["planes"]["backbone_pump_running"] is False


async def test_an_oversized_datagram_is_shed_before_any_parser(state: AppState) -> None:
    send_udp(state.backbone.local_addr[1], b"\x00" * (MAX_DATAGRAM + 1))
    await asyncio.sleep(0.05)
    assert state.backbone.dropped == 1
    assert not state.backbone_pump.done()


async def test_the_backbone_inbox_is_bounded(state: AppState, settings: Settings) -> None:
    """With the pump stopped, a flood is dropped and counted rather than buffered."""
    state.backbone_pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await state.backbone_pump

    send_udp(state.backbone.local_addr[1], b"relay copy", count=settings.cascade_inbox * 4)
    await asyncio.sleep(0.2)
    assert state.backbone.dropped > 0


async def test_the_media_socket_is_bound_and_advertised(state: AppState) -> None:
    host, port = state.media.local_addr
    assert host == "0.0.0.0"
    assert port != 0
    assert port != state.backbone.local_addr[1]


# --------------------------------------------------------------------------- #
# Config — fails at startup, not three layers into a consensus round
# --------------------------------------------------------------------------- #


def test_peers_parse_into_nodes() -> None:
    peers = parse_peers(
        "us-east=http://127.0.0.1:8081/|127.0.0.1:7101, ap-south=http://10.0.0.3:8080|10.0.0.3:7100"
    )
    assert peers == (
        PeerNode("us-east", "http://127.0.0.1:8081", ("127.0.0.1", 7101)),
        PeerNode("ap-south", "http://10.0.0.3:8080", ("10.0.0.3", 7100)),
    )
    assert parse_peers("") == ()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("us-east", id="no_equals"),
        pytest.param("us-east=http://h:8081", id="no_cascade_addr"),
        pytest.param("us-east=http://h:8081|h", id="cascade_addr_without_port"),
        pytest.param("us-east=http://h:8081|h:99999", id="port_out_of_range"),
        pytest.param("us-east=h:8081|h:7101", id="control_not_a_url"),
        pytest.param("us-east=http://a:1|a:1,us-east=http://b:1|b:1", id="region_twice"),
    ],
)
def test_malformed_peers_fail_at_startup(raw: str) -> None:
    """Strict on purpose: a silently dropped peer changes the quorum."""
    with pytest.raises(ValidationError):
        Settings(peers=raw)


def test_a_region_cannot_peer_with_itself() -> None:
    with pytest.raises(ValidationError):
        Settings(region="us-east", peers="us-east=http://h:8081|h:7101")


def test_heartbeat_must_be_below_the_election_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings(heartbeat_ms=1000, election_timeout_ms=1000)


def test_millisecond_knobs_become_seconds() -> None:
    config = Settings(election_timeout_ms=1500, heartbeat_ms=250, cluster_rpc_timeout_ms=100)
    assert (config.election_timeout, config.heartbeat, config.cluster_rpc_timeout) == (
        1.5,
        0.25,
        0.1,
    )


def test_an_ipv6_candidate_is_bracketed() -> None:
    assert Settings(public_ip=IPv6Address("::1")).advertised_media_addr(7000) == "[::1]:7000"


@pytest.mark.parametrize(
    ("peer_count", "quorum"),
    [(0, 1), (1, 2), (2, 2), (3, 3), (4, 3)],
)
def test_quorum_is_a_strict_majority_of_the_mesh(peer_count: int, quorum: int) -> None:
    config = PlacementConfig(
        region="eu-west",
        node_id="n1",
        peers=tuple(peer(f"r{i}", i) for i in range(peer_count)),
        election_timeout=1.0,
        heartbeat=0.3,
        max_rooms=4,
    )
    assert config.quorum == quorum


# --------------------------------------------------------------------------- #
# Wire shapes
# --------------------------------------------------------------------------- #


def test_room_placement_renders_regions_sorted() -> None:
    """Two nodes holding the same placement must render byte-identical JSON."""
    placement = RoomPlacement(
        room_id="all-hands",
        home_region="eu-west",
        active_regions=frozenset({"us-east", "ap-south", "eu-west"}),
    )
    assert placement.model_dump(mode="json")["active_regions"] == ["ap-south", "eu-west", "us-east"]


def test_log_entries_decode_through_the_discriminator() -> None:
    request = ReplicateRequest.model_validate(
        {
            "leader_id": "n2",
            "term": 3,
            "entries": [
                {"kind": "place_room", "room_id": "a", "region": "eu-west"},
                {"kind": "region_interest", "room_id": "a", "region": "us-east", "joined": False},
            ],
        }
    )
    first, second = request.entries
    assert isinstance(first, PlaceRoom)
    assert isinstance(second, RegionInterest)
    assert second.joined is False


# --------------------------------------------------------------------------- #
# The worklist, made executable. Delete each as you implement its vertical.
# --------------------------------------------------------------------------- #


async def test_v1_placement_is_unwritten() -> None:
    cluster = ClusterClient((), timeout=0.1)
    try:
        placement = Placement(
            PlacementConfig(
                region="eu-west",
                node_id="n1",
                peers=(),
                election_timeout=1.0,
                heartbeat=0.3,
                max_rooms=4,
            ),
            cluster,
        )
        # The read views are wired.
        assert placement.role is Role.FOLLOWER
        assert (placement.term, placement.leader) == (0, None)
        assert placement.snapshot() == []
        assert placement.room("all-hands") is None

        with pytest.raises(NotImplementedError):
            await placement.place_room("all-hands")
        with pytest.raises(NotImplementedError):
            await placement.register_interest("all-hands", "eu-west", joined=True)
        with pytest.raises(NotImplementedError):
            await placement.on_vote(VoteRequest(candidate_id="n2", term=1))
        with pytest.raises(NotImplementedError):
            await placement.on_replicate(ReplicateRequest(leader_id="n2", term=1))
        with pytest.raises(NotImplementedError):
            await placement.run()
        with pytest.raises(NotImplementedError):
            await placement.step_down()
    finally:
        await cluster.aclose()


async def test_v2_cascade_is_unwritten() -> None:
    mesh = CascadeMesh(
        CascadeConfig(region="eu-west", peers=(peer("us-east"),), max_legs=4),
        LayerRouter(RoutingConfig(hysteresis_ticks=3)),
    )
    assert mesh.legs() == []
    assert mesh.peer("us-east") == peer("us-east")
    assert mesh.peer("mars-1") is None

    track = ("all-hands", 1)
    with pytest.raises(NotImplementedError):
        await mesh.ensure_leg("us-east", track)
    with pytest.raises(NotImplementedError):
        await mesh.release_leg("us-east", track)
    with pytest.raises(NotImplementedError):
        mesh.relay_out(track, 0, b"rtp")
    with pytest.raises(NotImplementedError):
        mesh.on_relayed(("127.0.0.1", 7100), b"relay copy")
    with pytest.raises(NotImplementedError):
        await mesh.close_all()


def test_v3_router_is_unwritten() -> None:
    router = LayerRouter(RoutingConfig(hysteresis_ticks=3))
    assert router.snapshot() == []

    track = ("all-hands", 1)
    with pytest.raises(NotImplementedError):
        router.aggregate_local_demand(track, "us-east", [0, 2])
    with pytest.raises(NotImplementedError):
        router.recompute_leg(track, "us-east", frozenset({0, 2}))
    with pytest.raises(NotImplementedError):
        router.on_keyframe(track, 2)
    with pytest.raises(NotImplementedError):
        router.leg_carries(track, "us-east", 0)


async def test_v4_recorder_is_unwritten(tmp_path: Path) -> None:
    recorder = Recorder(RecordingConfig(directory=tmp_path, segment_secs=6.0))
    assert recorder.active() == []

    with pytest.raises(NotImplementedError):
        await recorder.start("all-hands")
    with pytest.raises(NotImplementedError):
        recorder.on_track_packet("all-hands", 1, b"rtp")
    with pytest.raises(NotImplementedError):
        await recorder.stop("all-hands")
    with pytest.raises(NotImplementedError):
        await recorder.flush_all()


def test_no_stray_collectors_in_the_registry() -> None:
    """Every `conf_*` series comes from `metrics.py`, so the names stay single-sourced."""
    names = {
        name
        for name in REGISTRY._names_to_collectors  # pyright: ignore[reportPrivateUsage] -- no public listing API
        if name.startswith("conf_")
    }
    assert "conf_relay_copies_out_total" in names
    assert "conf_node_role" in names
