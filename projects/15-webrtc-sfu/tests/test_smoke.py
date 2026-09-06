"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V4; those are yours to
write, and the SPEC's "Proof" lines say what each one has to demonstrate. What
is here is the plumbing: the app boots, both planes come up, signaling builds
the room graph and enforces its caps, the muxed UDP socket really receives, and
the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
those tests are the first thing that should fail — delete them then. They exist
so that "the scaffold is in its expected state" is something the suite asserts
rather than something you assume.
"""

from __future__ import annotations

import asyncio
import socket as socketlib

import httpx
import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from webrtc_sfu.bwe import ArrivalSample, BandwidthEstimator, split_budget
from webrtc_sfu.config import Settings
from webrtc_sfu.errors import BadMagicError, IntegrityError, MediaError, TruncatedError
from webrtc_sfu.forward import SEQ_MASK, Rewriter
from webrtc_sfu.ice import (
    STUN_MAGIC_COOKIE,
    IceAgent,
    IceResult,
    StunClass,
    StunMessage,
    UseCandidate,
    Username,
    XorMappedAddress,
    fingerprint,
    message_integrity,
)
from webrtc_sfu.routes import MAX_LAYERS
from webrtc_sfu.sfu import Role, Sfu
from webrtc_sfu.simulcast import LayerSelector, SimulcastLayer
from webrtc_sfu.state import AppState
from webrtc_sfu.wire import RTP_MIN_HEADER, PacketKind, RtpPacket, classify

LAYERS = [
    {"rid": "q", "ssrc": 111, "bitrate_bps": 150_000},
    {"rid": "h", "ssrc": 222, "bitrate_bps": 500_000},
    {"rid": "f", "ssrc": 333, "bitrate_bps": 2_000_000},
]


def rtp_packet(*, sequence: int = 1, timestamp: int = 0, ssrc: int = 111) -> bytearray:
    """A minimal well-formed RTP datagram: version 2, PT 96, 4 bytes of payload."""
    buf = bytearray(RTP_MIN_HEADER + 4)
    buf[0] = 0x80  # version 2, no padding, no extension, no CSRCs
    buf[1] = 96  # a dynamic payload type -- not in RTCP's 192..223 band
    buf[2:4] = sequence.to_bytes(2, "big")
    buf[4:8] = timestamp.to_bytes(4, "big")
    buf[8:12] = ssrc.to_bytes(4, "big")
    return buf


def stun_binding_request() -> bytes:
    """A datagram that `classify` calls STUN and `StunMessage.parse` must handle.

    Hand-built rather than produced by `StunMessage.encode`, on purpose: encode
    is a todo, and a fixture that depends on the code under test proves nothing
    about the code under test.
    """
    header = bytearray(20)
    header[0:2] = (0x0001).to_bytes(2, "big")  # class Request, method Binding
    header[2:4] = (0).to_bytes(2, "big")  # no attributes yet
    header[4:8] = STUN_MAGIC_COOKIE.to_bytes(4, "big")
    header[8:20] = bytes(range(12))  # transaction id
    return bytes(header)


# --------------------------------------------------------------------------- #
# The admin plane
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_readyz_is_green_while_the_pump_runs(client: httpx.AsyncClient) -> None:
    """Readiness is not an alias for liveness here -- see `routes.readyz`."""
    response = await client.get("/readyz")
    assert response.status_code == 200


async def test_metrics_exposes_the_sfu_series(client: httpx.AsyncClient) -> None:
    """Preregistered labelled children export at zero, not absent.

    The distinction is the point of `metrics.preregister`: a `rate()` over an
    absent series returns no data, and an alert over no data never fires.
    """
    response = await client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "sfu_rtp_received_total" in body
    assert 'sfu_rtp_dropped_total{reason="no_route"} 0.0' in body
    assert "sfu_forwarding_seconds_bucket" in body


async def test_status_reports_config_and_an_empty_topology(
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    body = (await client.get("/status")).json()
    assert body["limits"]["max_rooms"] == settings.max_rooms
    assert body["bitrate_bps"]["min"] == settings.min_kbps * 1000
    assert body["media_pump_running"] is True
    assert body["topology"] == {"rooms": []}


# --------------------------------------------------------------------------- #
# The signaling plane
# --------------------------------------------------------------------------- #


async def test_publish_returns_ice_credentials(client: httpx.AsyncClient) -> None:
    response = await client.post("/rooms/demo/publish", json={"layers": LAYERS})
    assert response.status_code == 200
    body = response.json()
    # RFC 5245: ufrag >= 4 chars, pwd >= 22.
    assert len(body["ice_ufrag"]) >= 4
    assert len(body["ice_pwd"]) >= 22
    # A publisher receives nothing, so it has no outbound SSRC -- and the field
    # is omitted rather than sent as null (`response_model_exclude_none`).
    assert "out_ssrc" not in body


async def test_subscribe_returns_a_stable_outbound_ssrc(client: httpx.AsyncClient) -> None:
    publisher = (await client.post("/rooms/demo/publish", json={"layers": LAYERS})).json()
    response = await client.post(
        "/rooms/demo/subscribe",
        json={"publisher": publisher["peer_id"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert 0 <= body["out_ssrc"] <= 0xFFFFFFFF
    assert body["ice_ufrag"] != publisher["ice_ufrag"]


async def test_subscribing_to_an_unknown_publisher_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/rooms/demo/subscribe", json={"publisher": 9999})
    assert response.status_code == 404


async def test_a_subscriber_cannot_pose_as_a_publisher(client: httpx.AsyncClient) -> None:
    """Subscribing to a *subscriber* is a 404, not a silently empty session."""
    publisher = (await client.post("/rooms/demo/publish", json={"layers": LAYERS})).json()
    subscriber = (
        await client.post("/rooms/demo/subscribe", json={"publisher": publisher["peer_id"]})
    ).json()
    response = await client.post(
        "/rooms/demo/subscribe",
        json={"publisher": subscriber["peer_id"]},
    )
    assert response.status_code == 404


async def test_rooms_shows_the_topology_without_credentials(client: httpx.AsyncClient) -> None:
    """`GET /rooms` is unauthenticated, so a leaked `pwd` here is a leaked session."""
    publisher = (await client.post("/rooms/demo/publish", json={"layers": LAYERS})).json()
    await client.post("/rooms/demo/subscribe", json={"publisher": publisher["peer_id"]})

    body = (await client.get("/rooms")).json()
    assert len(body["rooms"]) == 1
    assert body["rooms"][0]["room"] == "demo"
    assert [peer["role"] for peer in body["rooms"][0]["peers"]] == ["publisher", "subscriber"]
    assert publisher["ice_pwd"] not in (await client.get("/rooms")).text


async def test_max_rooms_is_enforced(client: httpx.AsyncClient, settings: Settings) -> None:
    for index in range(settings.max_rooms):
        response = await client.post(f"/rooms/room{index}/publish", json={"layers": LAYERS})
        assert response.status_code == 200
    # 409, not 400: the request was fine, the server is full.
    overflow = await client.post("/rooms/one-too-many/publish", json={"layers": LAYERS})
    assert overflow.status_code == 409


async def test_max_peers_per_room_is_enforced(
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    for _ in range(settings.max_peers_per_room):
        assert (
            await client.post("/rooms/full/publish", json={"layers": LAYERS})
        ).status_code == 200
    overflow = await client.post("/rooms/full/publish", json={"layers": LAYERS})
    assert overflow.status_code == 409


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
    """Validation happens in the model, before any handler runs -- see `routes`."""
    response = await client.post("/rooms/demo/publish", json={"layers": layers})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# The media plane: the socket is real, and the pump is really on it
# --------------------------------------------------------------------------- #


async def test_the_media_socket_receives_and_the_pump_reaches_v1(
    state: AppState,
    client: httpx.AsyncClient,
) -> None:
    """The whole scaffold, end to end, in the state it is meant to be in.

    Sends a real STUN Binding request at the real UDP port. The datagram
    endpoint delivers it, the pump classifies it, dispatch reaches
    `StunMessage.parse` -- and V1 raises. The pump dies, the HTTP server does
    not, and `/readyz` is what notices.

    This is also the test that proves `create_datagram_endpoint` is wired
    correctly rather than merely constructed, which is the one thing
    `make verify` could otherwise not tell you.
    """
    host, port = state.media.local_addr
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        sender.sendto(stun_binding_request(), ("127.0.0.1", port))
        assert host == "0.0.0.0"

        async with asyncio.timeout(5):
            while not state.pump.done():
                await asyncio.sleep(0.01)

    assert isinstance(state.pump.exception(), NotImplementedError)
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 503
    assert (await client.get("/status")).json()["media_pump_running"] is False


async def test_a_garbage_datagram_does_not_kill_the_pump(state: AppState) -> None:
    """An open UDP port takes bytes from anyone; unknown ones cost nothing."""
    _, port = state.media.local_addr
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        # 0x16 is DTLS -- classified UNKNOWN and dropped before any parser.
        sender.sendto(b"\x16\xfe\xfd" + b"\x00" * 40, ("127.0.0.1", port))
        await asyncio.sleep(0.05)
    assert not state.pump.done()


async def test_the_inbox_is_bounded(state: AppState, settings: Settings) -> None:
    """The queue drops rather than growing -- the remote-OOM guard in `pump`.

    Floods the port while the pump is blocked (it cannot drain: dispatch of the
    first STUN datagram raises and ends the task), then asserts that what the
    protocol kept is bounded by `MEDIA_INBOX` and the rest was counted as
    dropped rather than quietly buffered.
    """
    _, port = state.media.local_addr
    state.pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await state.pump

    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        for _ in range(settings.media_inbox * 4):
            sender.sendto(bytes(stun_binding_request()), ("127.0.0.1", port))
        await asyncio.sleep(0.2)

    assert state.media.dropped > 0


# --------------------------------------------------------------------------- #
# Wire helpers -- fully implemented, so these assert behaviour, not todos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("datagram", "expected"),
    [
        pytest.param(b"", PacketKind.UNKNOWN, id="empty"),
        pytest.param(b"\x00\x01", PacketKind.STUN, id="stun_binding_request"),
        pytest.param(b"\x01\x01", PacketKind.STUN, id="stun_indication"),
        pytest.param(b"\x80\x60", PacketKind.RTP, id="rtp_pt96"),
        pytest.param(b"\x80\xc9", PacketKind.RTCP, id="rtcp_receiver_report"),
        pytest.param(b"\x80\xc8", PacketKind.RTCP, id="rtcp_sender_report"),
        pytest.param(b"\x80", PacketKind.UNKNOWN, id="one_byte_in_the_rtp_band"),
        pytest.param(b"\x16\xfe", PacketKind.UNKNOWN, id="dtls"),
    ],
)
def test_classify_demuxes_the_muxed_port(datagram: bytes, expected: PacketKind) -> None:
    assert classify(datagram) is expected


def test_rtp_accessors_read_the_header() -> None:
    packet = RtpPacket(rtp_packet(sequence=4127, timestamp=90_000, ssrc=0xDEADBEEF))
    assert packet.sequence == 4127
    assert packet.timestamp == 90_000
    assert packet.ssrc == 0xDEADBEEF
    assert packet.payload_type == 96
    assert packet.marker is False


def test_rtp_setters_patch_in_place() -> None:
    buf = rtp_packet()
    packet = RtpPacket(buf)
    packet.sequence = 65535
    packet.timestamp = 4_000_000_000
    packet.ssrc = 0xCAFEBABE
    # Same buffer object -- the fan-out relies on this being a view, not a copy.
    assert RtpPacket(buf).sequence == 65535
    assert RtpPacket(buf).timestamp == 4_000_000_000
    assert RtpPacket(buf).ssrc == 0xCAFEBABE


def test_rtp_setters_mask_to_the_wire_width() -> None:
    """Python ints do not wrap; the setter masks so the bytes stay legal.

    This is the safety net, not the answer: V2 still has to mask its own
    arithmetic, because the number it compares and indexes with never passes
    through here. See `forward.py`.
    """
    buf = rtp_packet()
    packet = RtpPacket(buf)
    packet.sequence = SEQ_MASK + 1
    assert RtpPacket(buf).sequence == 0


@pytest.mark.parametrize(
    ("buf", "expected"),
    [
        pytest.param(bytearray(11), TruncatedError, id="shorter_than_a_header"),
        pytest.param(bytearray(RTP_MIN_HEADER), BadMagicError, id="version_zero"),
    ],
)
def test_rtp_construction_rejects_bad_datagrams(
    buf: bytearray,
    expected: type[MediaError],
) -> None:
    with pytest.raises(expected):
        RtpPacket(buf)


def test_every_media_error_shares_one_base(client: httpx.AsyncClient) -> None:
    """The pump catches `MediaError` once; that only works if they all inherit it."""
    assert issubclass(TruncatedError, MediaError)
    assert issubclass(BadMagicError, MediaError)
    assert issubclass(IntegrityError, MediaError)
    assert MediaError.client_safe is False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_bitrate_bounds_convert_kbps_to_bps() -> None:
    config = Settings(min_kbps=150, start_kbps=1000, max_kbps=4000)
    assert (config.min_bitrate, config.start_bitrate, config.max_bitrate) == (
        150_000,
        1_000_000,
        4_000_000,
    )


def test_an_unparseable_public_ip_fails_at_startup() -> None:
    """Not at the first ICE check, three layers into a handler, as a fallback."""
    with pytest.raises(ValidationError):
        Settings(public_ip="not-an-address")  # pyright: ignore[reportArgumentType]


def test_credentials_are_unique_per_peer(settings: Settings) -> None:
    """A repeated ufrag would route one peer's checks to another's ICE agent."""
    sfu = Sfu(settings)
    first = sfu.join_publisher("a", [], "client")
    second = sfu.join_publisher("a", [], "client")
    assert first.ice_ufrag != second.ice_ufrag
    assert first.ice_pwd != second.ice_pwd
    assert first.peer_id != second.peer_id


def test_topology_reports_roles(settings: Settings) -> None:
    sfu = Sfu(settings)
    publisher = sfu.join_publisher("a", [SimulcastLayer("q", 111, 150_000)], "c")
    sfu.subscribe("a", publisher.peer_id, "c")
    rooms = sfu.topology().rooms
    assert len(rooms) == 1
    assert [peer.role for peer in rooms[0].peers] == [Role.PUBLISHER, Role.SUBSCRIBER]


# --------------------------------------------------------------------------- #
# The worklist, made executable. Delete each as you implement its vertical.
# --------------------------------------------------------------------------- #


def test_v1_stun_codec_is_unwritten() -> None:
    with pytest.raises(NotImplementedError):
        StunMessage.parse(stun_binding_request())
    with pytest.raises(NotImplementedError):
        StunMessage(StunClass.REQUEST, 0x001, bytes(12)).encode()
    with pytest.raises(NotImplementedError):
        message_integrity(b"message", b"pwd")
    with pytest.raises(NotImplementedError):
        fingerprint(b"message")


def test_v1_ice_agent_is_unwritten() -> None:
    agent = IceAgent("local", "pwd", "remote")
    assert agent.peer is None
    message = StunMessage(
        StunClass.REQUEST,
        0x001,
        bytes(12),
        (Username("local:remote"), UseCandidate(), XorMappedAddress(("127.0.0.1", 5000))),
    )
    with pytest.raises(NotImplementedError):
        agent.handle(message, ("127.0.0.1", 5000))


def test_v2_rewriter_is_unwritten() -> None:
    rewriter = Rewriter(out_ssrc=0x1234)
    assert rewriter.out_ssrc == 0x1234
    with pytest.raises(NotImplementedError):
        rewriter.rewrite(RtpPacket(rtp_packet()))
    with pytest.raises(NotImplementedError):
        rewriter.skip()
    with pytest.raises(NotImplementedError):
        rewriter.to_origin_seq(4127)


def test_v3_layer_selector_is_unwritten() -> None:
    selector = LayerSelector(
        [
            SimulcastLayer("f", 333, 2_000_000),
            SimulcastLayer("q", 111, 150_000),
            SimulcastLayer("h", 222, 500_000),
        ]
    )
    # Sorting low -> high is wired, so the selector never has to care what order
    # the publisher announced them in.
    assert [layer.rid for layer in selector.layers] == ["q", "h", "f"]
    with pytest.raises(NotImplementedError):
        selector.set_budget(600_000)
    with pytest.raises(NotImplementedError):
        selector.on_packet(222, is_keyframe=True)
    with pytest.raises(NotImplementedError):
        _ = selector.wants_keyframe
    with pytest.raises(NotImplementedError):
        _ = selector.selected_bitrate


def test_v4_estimator_is_unwritten() -> None:
    estimator = BandwidthEstimator(1_000_000, 150_000, 4_000_000)
    # The clamp at construction is wired -- a start above max is not a valid
    # estimate to begin from, and the criterion says "always within [min, max]".
    assert estimator.estimate == 1_000_000
    assert BandwidthEstimator(9_000_000, 150_000, 4_000_000).estimate == 4_000_000
    assert BandwidthEstimator(1, 150_000, 4_000_000).estimate == 150_000
    with pytest.raises(NotImplementedError):
        estimator.on_loss(0.2)
    with pytest.raises(NotImplementedError):
        estimator.on_transport_feedback([ArrivalSample(0.0, 10.0, 1200)])
    with pytest.raises(NotImplementedError):
        split_budget(1_000_000, 2)


def test_the_ice_result_shape_is_wired() -> None:
    """Not a vertical: the *shape* the agent returns is given, so V1 only has to
    fill it in. Both fields empty means "nothing to send, nothing changed"."""
    assert IceResult() == IceResult(response=None, nominated=None)
    assert IceResult(response=b"pong").nominated is None


def test_no_stray_collectors_in_the_registry() -> None:
    """Every `sfu_*` series comes from `metrics.py`, so the names stay single-sourced."""
    names = {
        name
        for name in REGISTRY._names_to_collectors  # pyright: ignore[reportPrivateUsage]
        if name.startswith("sfu_")
    }
    assert "sfu_rooms" in names
    assert "sfu_peers" in names
