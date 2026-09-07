"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1–V4; those are yours to
write, and the SPEC's "Proof" lines name what each one has to demonstrate. What
is here is the plumbing: the app boots, both planes come up, the UDP socket
really receives, the media source really paces, and the unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
those tests are the first thing that should fail — delete them then. They exist
so that "the scaffold is in its expected state" is something the suite asserts
rather than something you assume.
"""

from __future__ import annotations

import asyncio
import socket as socketlib
import time
from typing import cast

import httpx
import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from media_transport.config import Role, Settings
from media_transport.congestion import CongestionController
from media_transport.errors import (
    BadVersionError,
    MalformedError,
    MediaError,
    TruncatedError,
)
from media_transport.jitter import JitterBuffer, JitterStats
from media_transport.media import SyntheticSource
from media_transport.rtcp import (
    BLP_BITS,
    RTCP_HEADER,
    Bye,
    Nack,
    ReceiverReport,
    RetransmitCache,
    pack_fci,
    parse_compound,
    serialize,
    unpack_fci,
)
from media_transport.rtp import (
    H264_CLOCK_RATE,
    RTP_MIN_HEADER,
    SEQ_MASK,
    Packetizer,
    RtpHeader,
    RtpPacket,
    depacketize,
)
from media_transport.state import AppState
from media_transport.udp import MAX_DATAGRAM


def rtp_datagram(*, sequence: int = 1, timestamp: int = 0, ssrc: int = 0xDEADBEEF) -> bytes:
    """A minimal well-formed RTP datagram: version 2, PT 96, 4 bytes of payload.

    Hand-built rather than produced by `RtpHeader.to_bytes`, on purpose: `to_bytes`
    is a todo, and a fixture that depends on the code under test proves nothing
    about the code under test.
    """
    buf = bytearray(RTP_MIN_HEADER + 4)
    buf[0] = 0x80  # version 2, no padding, no extension, no CSRCs
    buf[1] = 96  # a dynamic payload type
    buf[2:4] = sequence.to_bytes(2, "big")
    buf[4:8] = timestamp.to_bytes(4, "big")
    buf[8:12] = ssrc.to_bytes(4, "big")
    return bytes(buf)


# --------------------------------------------------------------------------- #
# The admin plane
# --------------------------------------------------------------------------- #


async def test_healthz_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


async def test_readyz_is_green_while_the_session_runs(client: httpx.AsyncClient) -> None:
    """Readiness is not an alias for liveness here — see `routes.readyz`."""
    assert (await client.get("/readyz")).status_code == 200


async def test_metrics_exposes_the_transport_series(client: httpx.AsyncClient) -> None:
    """Preregistered labelled children export at zero, not absent.

    The distinction is the point of `metrics.preregister`: a `rate()` over an
    absent series returns no data, and an alert over no data never fires.
    """
    body = (await client.get("/metrics")).text
    assert "media_transport_packets_received_total" in body
    assert 'media_transport_nacks_total{dir="sent"} 0.0' in body
    assert 'media_transport_datagrams_dropped_total{reason="inbox_full"} 0.0' in body
    assert "media_transport_playout_seconds_bucket" in body


async def test_status_reports_config_and_a_live_session(
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    body = (await client.get("/status")).json()
    assert body["role"] == "receiver"
    assert body["session_running"] is True
    assert body["session_error"] is None
    assert body["bitrate_bps"]["min"] == settings.min_kbps * 1000
    assert body["bounds"]["rtp_inbox"] == settings.rtp_inbox


# --------------------------------------------------------------------------- #
# The media plane: the socket is real, and the session is really on it
# --------------------------------------------------------------------------- #


async def test_the_socket_receives_and_the_session_reaches_v1(
    state: AppState,
    client: httpx.AsyncClient,
) -> None:
    """The whole scaffold, end to end, in the state it is meant to be in.

    Sends a real RTP datagram at the real UDP port. The datagram endpoint
    delivers it, the receive loop reaches `RtpPacket.parse` — and V1 raises. The
    session dies, the HTTP server does not, and `/readyz` is what notices.

    This is also the test that proves `create_datagram_endpoint` is wired
    correctly rather than merely constructed, which is the one thing
    `make verify` could otherwise not tell you.
    """
    host, port = state.media.local_addr
    assert host == "0.0.0.0"
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        sender.sendto(rtp_datagram(), ("127.0.0.1", port))
        async with asyncio.timeout(5):
            while not state.session.done():
                await asyncio.sleep(0.01)

    # The session runs its concerns in a TaskGroup, so the vertical's raise
    # arrives wrapped — which is exactly what `_log_session_exit` unwraps.
    error = state.session.exception()
    assert isinstance(error, BaseExceptionGroup)
    group = cast(BaseExceptionGroup[BaseException], error)
    assert isinstance(group.exceptions[0], NotImplementedError)

    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 503
    body = (await client.get("/status")).json()
    assert body["session_running"] is False
    assert "NotImplementedError" in body["session_error"]


async def test_an_oversized_datagram_is_dropped_before_parsing(state: AppState) -> None:
    """`MAX_DATAGRAM` bounds what we keep, not what the kernel accepts."""
    _, port = state.media.local_addr
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        sender.sendto(b"\x80\x60" + bytes(MAX_DATAGRAM + 100), ("127.0.0.1", port))
        await asyncio.sleep(0.05)
    assert state.media.dropped > 0
    # Dropped in the protocol callback, so it never reached a parser and the
    # session is untouched.
    assert not state.session.done()


async def test_the_inbox_is_bounded(state: AppState, settings: Settings) -> None:
    """The queue drops rather than growing — the remote-OOM guard in `udp`.

    Floods the port while nothing is draining (the session is cancelled first),
    then asserts that what the protocol kept is bounded by `RTP_INBOX` and the
    rest was counted as dropped rather than quietly buffered.
    """
    _, port = state.media.local_addr
    state.session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await state.session

    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM) as sender:
        for sequence in range(settings.rtp_inbox * 8):
            sender.sendto(rtp_datagram(sequence=sequence & SEQ_MASK), ("127.0.0.1", port))
        await asyncio.sleep(0.2)

    assert state.media.dropped > 0


# --------------------------------------------------------------------------- #
# The synthetic source — wired, so these assert behaviour, not todos
# --------------------------------------------------------------------------- #


async def test_the_source_sizes_frames_from_the_bitrate() -> None:
    """The feedback loop closing: a backed-off estimate makes smaller frames."""
    source = SyntheticSource(fps=30, gop=60)
    fat = await source.next_frame(1_500_000)
    lean = await source.next_frame(300_000)
    assert len(fat.data) == (1_500_000 // 8) // 30
    assert len(lean.data) == (300_000 // 8) // 30


async def test_the_source_marks_keyframes_and_advances_the_media_clock() -> None:
    """Timestamps step by the sampling interval, not per packet — the V1 lesson."""
    source = SyntheticSource(fps=30, gop=2)
    frames = [await source.next_frame(300_000) for _ in range(4)]
    assert [frame.keyframe for frame in frames] == [True, False, True, False]
    ticks = H264_CLOCK_RATE // 30
    assert [frame.rtp_timestamp for frame in frames] == [0, ticks, 2 * ticks, 3 * ticks]


async def test_the_source_paces_without_drifting() -> None:
    """An absolute deadline, not `sleep(1/fps)` in a loop — see `media.py`."""
    source = SyntheticSource(fps=100, gop=60)
    started = time.monotonic()
    for _ in range(5):
        await source.next_frame(300_000)
    # Five frames at 100 fps is ~40 ms of intervals after the first; generous
    # upper bound so a loaded CI box does not make this flaky.
    assert 0.03 <= time.monotonic() - started < 0.5


# --------------------------------------------------------------------------- #
# Config — the type annotation is the parser
# --------------------------------------------------------------------------- #


def test_bitrate_bounds_convert_kbps_to_bps() -> None:
    config = Settings(min_kbps=300, start_kbps=1500, max_kbps=4000)
    assert (config.min_bitrate, config.start_bitrate, config.max_bitrate) == (
        300_000,
        1_500_000,
        4_000_000,
    )


def test_playout_is_exposed_in_seconds() -> None:
    """One time unit in the process — see `Settings.target_playout`."""
    assert Settings(playout_ms=100).target_playout == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("127.0.0.1:5004", ("127.0.0.1", 5004), id="ipv4"),
        pytest.param("[::1]:5004", ("::1", 5004), id="ipv6_bracketed"),
        pytest.param("host.example:9", ("host.example", 9), id="hostname"),
        pytest.param("127.0.0.1", None, id="no_port"),
        pytest.param("127.0.0.1:not-a-port", None, id="bad_port"),
        pytest.param("", None, id="empty"),
    ],
)
def test_remote_addr_parses_once(value: str, expected: tuple[str, int] | None) -> None:
    assert Settings(remote_addr=value).remote == expected


def test_an_unknown_role_fails_at_startup() -> None:
    """Not silently defaulted to receiver — see the `config` module docstring."""
    with pytest.raises(ValidationError):
        Settings(role="sendr")  # pyright: ignore[reportArgumentType]


def test_a_payload_type_out_of_range_fails_at_startup() -> None:
    """7 bits on the wire, so 200 is not a payload type however you feel about it."""
    with pytest.raises(ValidationError):
        Settings(payload_type=200)


# --------------------------------------------------------------------------- #
# Errors — one base class is what makes the loop's single `except` work
# --------------------------------------------------------------------------- #


def test_every_media_error_shares_one_base() -> None:
    assert issubclass(TruncatedError, MediaError)
    assert issubclass(BadVersionError, MediaError)
    assert issubclass(MalformedError, MediaError)
    assert MediaError.client_safe is False


def test_truncated_carries_what_it_needed() -> None:
    """The detail is for the log, never for a caller — see `errors.py`."""
    error = TruncatedError(need=12, got=7)
    assert (error.need, error.got) == (12, 7)
    assert "12" in str(error) and "7" in str(error)


# --------------------------------------------------------------------------- #
# The worklist, made executable. Delete each as you implement its vertical.
# --------------------------------------------------------------------------- #


def test_v1_rtp_codec_is_unwritten() -> None:
    # The guards that *are* wired: a short buffer and a bad version never reach
    # the todo, because a parser on an open port has to reject those first.
    with pytest.raises(TruncatedError):
        RtpHeader.parse(bytes(RTP_MIN_HEADER - 1))
    with pytest.raises(BadVersionError):
        RtpHeader.parse(bytes(RTP_MIN_HEADER))

    with pytest.raises(NotImplementedError):
        RtpHeader.parse(rtp_datagram())
    header = RtpHeader(marker=True, payload_type=96, sequence=1, timestamp=0, ssrc=7)
    with pytest.raises(NotImplementedError):
        header.to_bytes()
    with pytest.raises(NotImplementedError):
        RtpPacket.parse(rtp_datagram())
    with pytest.raises(NotImplementedError):
        RtpPacket(header, b"payload").to_bytes()
    with pytest.raises(NotImplementedError):
        depacketize([RtpPacket(header, b"payload")])


def test_v1_header_length_is_wired() -> None:
    """Not a vertical: the payload offset is a pure function of the CSRC count,
    which is why `parse` returns one value and not a tuple."""
    plain = RtpHeader(marker=False, payload_type=96, sequence=0, timestamp=0, ssrc=1)
    assert plain.byte_length == RTP_MIN_HEADER
    mixed = RtpHeader(
        marker=False, payload_type=96, sequence=0, timestamp=0, ssrc=1, csrc=(2, 3, 4)
    )
    assert mixed.byte_length == RTP_MIN_HEADER + 12


def test_v1_headers_compare_by_value() -> None:
    """The round-trip criterion is an `==`, so it had better be a value type."""
    first = RtpHeader(marker=True, payload_type=96, sequence=9, timestamp=90, ssrc=1)
    second = RtpHeader(marker=True, payload_type=96, sequence=9, timestamp=90, ssrc=1)
    assert first == second
    assert len({first, second}) == 1


def test_v1_packetizer_rejects_an_mtu_that_cannot_hold_a_header() -> None:
    """Wired, because it is a configuration error rather than a wire one."""
    from media_transport.errors import OversizedError

    with pytest.raises(OversizedError):
        Packetizer(ssrc=1, payload_type=96, mtu=RTP_MIN_HEADER, initial_sequence=0)


def test_v1_packetize_is_unwritten() -> None:
    packetizer = Packetizer(ssrc=1, payload_type=96, mtu=1200, initial_sequence=65_535)
    # The sequence is masked at construction, so V1 starts from a legal wire
    # value even when handed a Python int that is not one.
    assert packetizer.next_sequence == 65_535
    assert Packetizer(1, 96, 1200, SEQ_MASK + 5).next_sequence == 4
    with pytest.raises(NotImplementedError):
        packetizer.packetize(b"access unit", 90_000)


def test_v2_jitter_buffer_is_unwritten() -> None:
    buffer = JitterBuffer(target_delay=0.1, clock_rate=H264_CLOCK_RATE, capacity=64)
    # Emptiness is wired so the playout tick can idle without raising.
    assert not buffer
    assert len(buffer) == 0
    assert buffer.stats == JitterStats()

    header = RtpHeader(marker=True, payload_type=96, sequence=1, timestamp=0, ssrc=1)
    with pytest.raises(NotImplementedError):
        buffer.insert(RtpPacket(header, b"payload"), time.monotonic())
    with pytest.raises(NotImplementedError):
        buffer.pop_frame(time.monotonic())
    with pytest.raises(NotImplementedError):
        buffer.missing()


def test_v3_rtcp_codec_is_unwritten() -> None:
    with pytest.raises(TruncatedError):
        parse_compound(bytes(RTCP_HEADER - 1))
    with pytest.raises(NotImplementedError):
        parse_compound(bytes(RTCP_HEADER * 2))
    with pytest.raises(NotImplementedError):
        pack_fci([1, 2, 3])
    with pytest.raises(NotImplementedError):
        unpack_fci([0x0001_0003])
    with pytest.raises(NotImplementedError):
        Nack.from_missing(1, 2, [4127]).fci()
    with pytest.raises(NotImplementedError):
        serialize(Bye((1,)))
    with pytest.raises(NotImplementedError):
        serialize(
            ReceiverReport(
                reporter_ssrc=1,
                media_ssrc=2,
                fraction_lost=0,
                cumulative_lost=0,
                highest_sequence=0,
                jitter=0,
            )
        )


def test_v3_nack_holds_its_missing_set_immutably() -> None:
    """Wired: a request already on the wire must not be mutable behind its back."""
    nack = Nack.from_missing(1, 2, [7, 9, 11])
    assert nack.lost == (7, 9, 11)
    assert BLP_BITS == 16


def test_v3_retransmit_cache_is_unwritten() -> None:
    cache = RetransmitCache(capacity=4)
    assert cache.capacity == 4
    assert len(cache) == 0
    header = RtpHeader(marker=False, payload_type=96, sequence=1, timestamp=0, ssrc=1)
    with pytest.raises(NotImplementedError):
        cache.record(RtpPacket(header, b"payload"))
    with pytest.raises(NotImplementedError):
        cache.get(1)


def test_v4_controller_is_unwritten() -> None:
    controller = CongestionController(1_500_000, 300_000, 4_000_000)
    # The clamp at construction is wired — a start outside the bounds is not a
    # valid estimate to begin from, and the criterion says "always within
    # [min, max]" with no exception for the first second of the run.
    assert controller.target_bitrate == 1_500_000
    assert controller.bounds == (300_000, 4_000_000)
    assert CongestionController(9_000_000, 300_000, 4_000_000).target_bitrate == 4_000_000
    assert CongestionController(1, 300_000, 4_000_000).target_bitrate == 300_000

    with pytest.raises(NotImplementedError):
        controller.on_receiver_report(0.2, 0.005)
    with pytest.raises(NotImplementedError):
        controller.on_delay_sample(0.010, 0.015)
    with pytest.raises(NotImplementedError):
        controller.delay_before(time.monotonic(), 1200)
    with pytest.raises(NotImplementedError):
        controller.on_sent(1200)


def test_no_stray_collectors_in_the_registry() -> None:
    """Every `media_transport_*` series comes from `metrics.py`, single-sourced."""
    names = {
        name
        for name in REGISTRY._names_to_collectors  # pyright: ignore[reportPrivateUsage]
        if name.startswith("media_transport_")
    }
    assert "media_transport_jitter_buffer_depth" in names
    assert "media_transport_target_bitrate_bps" in names


def test_a_sender_without_a_remote_addr_fails_loudly() -> None:
    """Named at startup, not as a `TypeError` on a `None` address 20 packets in."""
    assert Settings(role=Role.SENDER, remote_addr=None).remote is None
