"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V6; those are yours to
write, and the SPEC's "Proof" lines say what each one has to demonstrate. What
is here is the plumbing: the app boots, the control plane answers, config parses
and does not publish this client's identity, the peer listener accepts, and the
unbuilt parts raise.

That last group is the worklist made executable. When you implement a vertical,
those tests are the first thing that should fail — delete them then. They exist
so that "the scaffold is in its expected state" is something the suite asserts
rather than something you assume.
"""

from __future__ import annotations

import asyncio
import socket
from collections import Counter
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from pydantic import ValidationError

from bittorrent import metrics
from bittorrent.bencode import MAX_DEPTH, decode, decode_with_spans, encode
from bittorrent.client import PEER_ID_PREFIX, Client, TorrentStatus, generate_peer_id
from bittorrent.config import Settings
from bittorrent.download import PieceStore, pick_piece, verify_piece
from bittorrent.errors import (
    AppError,
    BadRequest,
    BencodeError,
    InvalidTorrent,
    NotFound,
    PeerError,
    StorageError,
    TrackerError,
)
from bittorrent.metainfo import MagnetLink, Metainfo, safe_relative_path
from bittorrent.peer import (
    BLOCK_SIZE,
    HANDSHAKE_LEN,
    PROTOCOL,
    Handshake,
    Have,
    MessageId,
    PeerState,
    Unknown,
    decode_message,
    encode_message,
    pack_bitfield,
    unpack_bitfield,
)
from bittorrent.routes import MAX_TORRENT_BYTES
from bittorrent.seeder import Seeder, UnchokeCandidate, choke_loop, pick_optimistic, select_unchoked
from bittorrent.state import AppState
from bittorrent.tracker import (
    COMPACT_PEER_LEN,
    UDP_PROTOCOL_MAGIC,
    AnnounceRequest,
    Event,
    parse_compact_peers,
    udp_channel,
)
from bittorrent.types import HASH_LEN, InfoHash, PeerId
from tests.conftest import PeerClient

ZERO_HASH = InfoHash(bytes(HASH_LEN))


# --- the control plane --------------------------------------------------------


async def test_healthz(http: httpx.AsyncClient) -> None:
    response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(http: httpx.AsyncClient) -> None:
    response = await http.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


async def test_every_series_exports_at_zero_before_anything_happens(
    http: httpx.AsyncClient,
) -> None:
    """Absent is not zero.

    Without pre-registering the label values, `bt_pieces_verified_total` with
    `result="failed"` does not exist until the first lying peer — so a dashboard
    shows a gap and an alert cannot distinguish "no failures" from "not
    reporting". The label sets here are closed (two outcomes, two transports),
    which is exactly what makes enumerating them possible.
    """
    body = (await http.get("/metrics")).text
    assert 'bt_pieces_verified_total{result="ok"} 0.0' in body
    assert 'bt_pieces_verified_total{result="failed"} 0.0' in body
    assert 'bt_tracker_announces_total{result="ok",transport="udp"} 0.0' in body
    assert "bt_bytes_downloaded_total 0.0" in body
    assert "bt_peers_unchoked 0.0" in body


async def test_request_id_header_is_echoed(http: httpx.AsyncClient) -> None:
    response = await http.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


async def test_torrent_list_starts_empty(http: httpx.AsyncClient) -> None:
    response = await http.get("/torrents")
    assert response.status_code == 200
    assert response.json() == []


# --- security: this client's identity is not published ------------------------


async def test_config_endpoint_does_not_leak_the_peer_id(
    settings: Settings, http: httpx.AsyncClient
) -> None:
    """The SPEC's security item, as a test rather than a promise.

    `public_status` is an allowlist, so this passes today *and* keeps passing
    when someone adds a second sensitive field — which is the whole reason it is
    an allowlist rather than `model_dump()` minus a denylist.
    """
    response = await http.get("/config")
    assert response.status_code == 200
    body = response.json()
    assert "peer_id" not in body
    assert set(body) == set(settings.public_status())


def test_peer_id_is_well_formed_and_stable_per_run() -> None:
    """The horizontal item: a client prefix plus random bytes, 20 long, stable
    for the run — and *different* between runs, which is what makes it an
    identity rather than a constant."""
    peer_id = generate_peer_id()
    assert len(peer_id.raw) == HASH_LEN
    assert peer_id.raw.startswith(PEER_ID_PREFIX)
    assert generate_peer_id().raw != peer_id.raw

    client = Client(Settings(), peer_id=peer_id)
    assert client.peer_id.raw == peer_id.raw


# --- identity types -----------------------------------------------------------


def test_infohash_round_trips_through_hex() -> None:
    raw = bytes(range(HASH_LEN))
    info_hash = InfoHash(raw)
    assert info_hash.hex() == raw.hex()
    assert len(info_hash.hex()) == 40
    assert InfoHash.from_hex(info_hash.hex()) == info_hash


def test_infohash_rejects_anything_that_is_not_twenty_bytes() -> None:
    """The invariant checked at the boundary that builds it. A 19-byte infohash
    produces a handshake a peer silently drops, which is an hour with a packet
    capture instead of an exception here."""
    with pytest.raises(ValueError):
        InfoHash(b"too short")
    with pytest.raises(ValueError):
        PeerId(bytes(21))
    assert InfoHash.from_hex("not hex at all") is None
    assert InfoHash.from_hex("ab" * 19) is None


def test_infohash_is_hashable_so_it_can_name_a_torrent() -> None:
    """Content-addressing only works if identity is a usable key: equal bytes are
    the same torrent, no matter which parse produced them."""
    assert {InfoHash(bytes(HASH_LEN)): "a"}[InfoHash(bytes(HASH_LEN))] == "a"


def test_infohash_renders_as_hex_in_json() -> None:
    """Raw bytes are for the wire, hex is for JSON — and getting that backwards
    in a response body is the bug this custom schema exists to prevent."""
    status = TorrentStatus(info_hash=ZERO_HASH, name="x", total_length=0)
    assert status.model_dump(mode="json")["info_hash"] == "00" * HASH_LEN


# --- config -------------------------------------------------------------------


def test_ports_are_validated_at_startup() -> None:
    """`PEER_PORT=0` is legal and means "any free port"; a control-plane port of
    0 is not, because nothing would know where to find it."""
    assert Settings(peer_port=0).peer_port == 0
    with pytest.raises(ValidationError):
        Settings(port=0)
    with pytest.raises(ValidationError):
        Settings(peer_port=70000)


def test_caps_must_be_positive() -> None:
    """A cap of zero is not "unlimited", it is a client that can do nothing —
    and silently defaulting it would be a resource bound that quietly is not
    one."""
    with pytest.raises(ValidationError):
        Settings(max_peers=0)
    with pytest.raises(ValidationError):
        Settings(upload_slots=0)
    with pytest.raises(ValidationError):
        Settings(pipeline_depth=0)
    with pytest.raises(ValidationError):
        Settings(disk_workers=0)
    with pytest.raises(ValidationError):
        Settings(max_message_bytes=0)


# --- errors -------------------------------------------------------------------


def test_client_mistakes_keep_their_message() -> None:
    """A caller who cannot tell "not a torrent file" from "no such infohash"
    cannot fix either."""
    assert BadRequest("bad magnet").status_code == 400
    assert BencodeError().status_code == 400
    assert InvalidTorrent("piece count mismatch").message == "piece count mismatch"
    assert NotFound().status_code == 404
    assert BadRequest().client_safe


def test_network_faults_never_describe_the_swarm() -> None:
    """`PeerError` knows a peer's address and `TrackerError` a tracker's URL. A
    caller learns neither — that is a map of the swarm, and it belongs in a log
    with a retention policy."""
    for error in (TrackerError(), PeerError(), StorageError()):
        assert error.status_code == 500
        assert not error.client_safe
    assert AppError.message == "internal server error"


# --- the control plane's guards, which need no vertical -----------------------


async def test_empty_and_oversized_torrent_bodies_are_rejected(http: httpx.AsyncClient) -> None:
    """Bound before you allocate, applied at the one place an untrusted body
    enters over HTTP. Both checks matter: the header is a claim, and a chunked
    upload makes no claim at all."""
    empty = await http.post("/torrents", content=b"")
    assert empty.status_code == 400

    oversized = await http.post("/torrents", content=b"d" * (MAX_TORRENT_BYTES + 1))
    assert oversized.status_code == 400
    assert "error" in oversized.json()


async def test_unknown_and_malformed_infohashes(http: httpx.AsyncClient) -> None:
    assert (await http.get("/torrents/not-hex")).status_code == 400
    assert (await http.get(f"/torrents/{ZERO_HASH.hex()}")).status_code == 404


async def test_a_magnet_with_an_empty_uri_is_rejected_before_the_parser(
    http: httpx.AsyncClient,
) -> None:
    """pydantic's `min_length` catches this, so it is a 422 from validation
    rather than a trip into V2 — which is the right layer for "you sent me
    nothing"."""
    response = await http.post("/torrents/magnet", json={"uri": ""})
    assert response.status_code == 422


# --- the registry, which is wired ---------------------------------------------


def test_progress_is_counted_in_verified_pieces(engine: Client) -> None:
    """Bytes that arrived but failed their hash are not progress. Counting
    pieces rather than bytes makes that true by construction."""
    status = TorrentStatus(info_hash=ZERO_HASH, name="x", total_length=100, total_pieces=4)
    assert status.progress == 0.0
    status.have_pieces = 3
    assert status.progress == 0.75
    assert TorrentStatus(info_hash=ZERO_HASH, name="x", total_length=0).progress == 0.0
    assert engine.get(ZERO_HASH) is None


def test_adding_the_same_torrent_twice_is_idempotent(engine: Client) -> None:
    """The infohash *is* the identity, so a second `.torrent` for the same
    content names a download already running. Replacing the entry would discard
    live progress."""
    first = TorrentStatus(info_hash=ZERO_HASH, name="first", total_length=10)
    assert engine.register(first) == ZERO_HASH
    again = TorrentStatus(info_hash=ZERO_HASH, name="second", total_length=99)
    assert engine.register(again) == ZERO_HASH
    assert len(engine.status()) == 1
    assert engine.status()[0].name == "first"


# --- protocol constants, which are constants ----------------------------------


def test_the_handshake_is_sixty_eight_bytes() -> None:
    """1 + 19 + 8 + 20 + 20. Derived from the parts rather than written as 68, so
    the two cannot drift apart."""
    assert HANDSHAKE_LEN == 68
    assert len(PROTOCOL) == 19
    assert BLOCK_SIZE == 16 * 1024
    assert COMPACT_PEER_LEN == 6
    assert UDP_PROTOCOL_MAGIC == 0x41727101980


def test_message_ids_match_the_spec() -> None:
    assert MessageId.CHOKE == 0
    assert MessageId.PIECE == 7
    assert MessageId.CANCEL == 8


def test_announce_events_carry_their_wire_spelling() -> None:
    """`Event.NONE` is the empty string over HTTP — and note the UDP ordinals are
    a *different* mapping, which is V3's to get right."""
    assert Event.NONE == ""
    assert Event.STARTED == "started"
    assert Event.STOPPED == "stopped"
    assert (
        AnnounceRequest(
            info_hash=ZERO_HASH,
            peer_id=generate_peer_id(),
            port=6819,
            uploaded=0,
            downloaded=0,
            left=100,
        ).event
        is Event.NONE
    )


def test_a_peer_connection_starts_choked_and_uninterested() -> None:
    """Nothing flows on a fresh connection until two messages have been
    exchanged. That is the protocol, not an accident of the defaults."""
    state = PeerState()
    assert state.am_choking and state.peer_choking
    assert not state.am_interested and not state.peer_interested
    assert state.has == set()


def test_unknown_messages_are_a_value_not_an_exception() -> None:
    """Forward compatibility: an id this client does not implement must be
    representable, so ignoring it is a `match` arm rather than something you have
    to remember not to raise."""
    unknown = Unknown(id=99, body=b"payload")
    assert unknown.id == 99
    assert Have(index=3) != Have(index=4)


# --- the seeder's listener ----------------------------------------------------


async def test_the_peer_port_accepts_connections(seeder: Seeder) -> None:
    """The accept loop is real before the sessions are: a peer can connect and
    the connection stays open. That is what makes the *next* test's failure
    informative — it isolates "V4/V6 is unwritten" from "nothing is
    listening"."""
    assert seeder.port > 0
    with socket.create_connection(("127.0.0.1", seeder.port), timeout=2) as sock:
        assert sock.fileno() > 0


async def test_nothing_is_unchoked_before_the_choke_policy_exists(seeder: Seeder) -> None:
    """The gauge the boss fight watches, on an idle seeder. A slot handed out by
    default would make the cap decorative."""
    assert seeder.unchoked_count == 0


async def test_an_inbound_peer_trips_the_unbuilt_session(peer_client: PeerClient) -> None:
    """The worklist, made executable.

    A handshake reaches `serve_peer`, which raises, which ends that session's
    task — while the seeder keeps serving. Delete this test the moment V6 exists;
    it should be the first thing that fails.
    """
    await peer_client.send(bytes([19]) + PROTOCOL + bytes(8) + bytes(20) + bytes(20))
    assert await peer_client.read() == b"", "V6 is implemented — assert on the handshake reply"


async def test_the_seeder_survives_a_session_that_died(
    seeder: Seeder, seeding_app: FastAPI
) -> None:
    """One peer tripping an unbuilt vertical must not take the process down —
    the property that makes the scaffold usable at all."""
    reader, writer = await asyncio.open_connection("127.0.0.1", seeder.port)
    writer.write(b"\x13" + PROTOCOL + bytes(48))
    await writer.drain()
    # The session raises before reading these bytes, so the close comes back as
    # an RST rather than a FIN — see `PeerClient.read`. Either is a peer that
    # hung up, and neither is what this test is about.
    with suppress(ConnectionResetError):
        await reader.read(1024)
    writer.close()
    with suppress(ConnectionResetError, OSError):
        await writer.wait_closed()

    transport = httpx.ASGITransport(app=seeding_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bt") as client:
        assert (await client.get("/healthz")).status_code == 200

    reader2, writer2 = await asyncio.open_connection("127.0.0.1", seeder.port)
    assert reader2 is not None
    writer2.close()
    with suppress(ConnectionResetError, OSError):
        await writer2.wait_closed()


# --- the choke loop -----------------------------------------------------------


async def test_choke_loop_survives_the_unbuilt_policy(
    seeder: Seeder, seeding_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must tolerate the policy raising: on the scaffold it does, every
    round, and a scheduler that died on the first tick would look exactly like
    one that was working — until the boss fight, where the unchoke set silently
    never changes."""
    monkeypatch.setattr("bittorrent.seeder.UNCHOKE_INTERVAL", 0.01)
    task = asyncio.create_task(choke_loop(seeder, seeding_settings))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_choke_loop_stops_on_cancel(seeder: Seeder, seeding_settings: Settings) -> None:
    """`CancelledError` inherits from `BaseException`, so the loop's
    `except Exception` does not swallow it. If it did, shutdown would hang."""
    task = asyncio.create_task(choke_loop(seeder, seeding_settings))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- the UDP plumbing, which is wired because uvloop demands it ---------------


async def test_udp_channel_round_trips_a_datagram() -> None:
    """The wired half of V3. `create_datagram_endpoint` works on both event loops
    this project runs on; `loop.sock_recvfrom` does not exist under uvloop, which
    is why this plumbing is not left as an exercise."""
    loop = asyncio.get_running_loop()
    echoes: asyncio.Queue[bytes] = asyncio.Queue()

    class Echo(asyncio.DatagramProtocol):
        def __init__(self) -> None:
            self.transport: asyncio.DatagramTransport | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, asyncio.DatagramTransport)
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
            echoes.put_nowait(data)
            if self.transport is not None:
                self.transport.sendto(b"pong", addr)  # type: ignore[arg-type]  # asyncio's addr

    server, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
    port = int(server.get_extra_info("sockname")[1])
    try:
        async with udp_channel(("127.0.0.1", port)) as channel:
            channel.send(b"ping")
            assert await channel.receive(timeout=2.0) == b"pong"
            assert channel.dropped == 0
        assert await echoes.get() == b"ping"
    finally:
        server.close()


async def test_udp_receive_times_out_rather_than_waiting_forever() -> None:
    """A tracker that is down, a firewall eating your packets, and a slow tracker
    are the same observation on UDP. Only a deadline turns the first two into
    something you can retry past."""
    async with udp_channel(("127.0.0.1", 1)) as channel:
        with pytest.raises(TimeoutError):
            await channel.receive(timeout=0.05)


# --- metrics ------------------------------------------------------------------


def test_ttfb_buckets_bracket_the_boss_fight_target() -> None:
    """A histogram whose buckets do not bracket the SLO is a metric that cannot
    fail. The target is p99 time-to-first-block <= 250 ms, so 0.25 has to be a
    bucket edge — and the low end has to be dense enough to tell a loopback run
    that worked from one where the storm never formed."""
    assert 0.25 in metrics.TTFB_BUCKETS
    assert min(metrics.TTFB_BUCKETS) <= 0.005


def test_verified_pieces_are_two_counters_not_a_ratio() -> None:
    """Counters per outcome, never a pre-computed ratio: a ratio cannot be
    aggregated across replicas or re-windowed after the fact, and two counters
    can."""
    metrics.PIECES_VERIFIED_TOTAL.labels(result="ok")
    metrics.PIECES_VERIFIED_TOTAL.labels(result="failed")
    with pytest.raises(ValueError):
        metrics.PIECES_VERIFIED_TOTAL.labels(result="ok", peer="1.2.3.4")

    exported = {metric.name for metric in REGISTRY.collect()}
    assert "bt_pieces_verified" in exported
    assert "bt_peers_unchoked" in exported
    assert not [name for name in exported if name.endswith("_ratio")]


def test_no_metric_is_labelled_by_peer_address() -> None:
    """The obvious next label, and a cardinality bomb — fifty series per metric
    from the boss fight alone, unbounded on a public swarm. It is also the
    security item: a scrape endpoint that enumerates the swarm deanonymizes
    everyone in it."""
    for metric in REGISTRY.collect():
        if not metric.name.startswith("bt_"):
            continue
        for sample in metric.samples:
            assert "peer" not in sample.labels
            assert "addr" not in sample.labels


# --- graceful shutdown --------------------------------------------------------


async def test_lifespan_stops_accepting_inbound_peers(seeding_settings: Settings) -> None:
    """The graceful-shutdown criterion's wired half: leaving the lifespan closes
    the peer listener. The other half — announcing `stopped` to every tracker —
    is V3's, and lives in the test you write once announces exist."""
    from bittorrent.main import create_app

    app = create_app(seeding_settings)
    async with app.router.lifespan_context(app):
        state = getattr(app.state, "app_state", None)
        assert isinstance(state, AppState)
        listener = state.seeder
        assert isinstance(listener, Seeder)
        port = listener.port
        # Connected through asyncio rather than a blocking socket so the close
        # is *awaited*: that gives the loop the turns it needs to run the
        # server-side session's teardown before the lifespan tears the listener
        # down underneath it. A raw `socket.create_connection` leaves the
        # server's transport to be finalized by the garbage collector later,
        # which asyncio complains about long after this test has passed.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert reader is not None
        writer.close()
        with suppress(ConnectionResetError, OSError):
            await writer.wait_closed()

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


async def test_a_leech_only_client_has_no_seeder(app: FastAPI) -> None:
    """`RUN_SEEDER=false` is the scaffold's default and a legitimate way to run:
    `None` is a real state, not a missing value."""
    state = getattr(app.state, "app_state", None)
    assert isinstance(state, AppState)
    assert state.seeder is None


# --- the worklist: everything below raises until you build it -----------------


def test_bencode_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        decode(b"i42e")
    with pytest.raises(NotImplementedError):
        encode(b"spam")
    with pytest.raises(NotImplementedError):
        decode_with_spans(b"de")
    assert MAX_DEPTH > 0


def test_metainfo_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        Metainfo.from_bytes(b"d4:infod4:name1:aee")
    with pytest.raises(NotImplementedError):
        MagnetLink.parse("magnet:?xt=urn:btih:" + "ab" * 20)
    with pytest.raises(NotImplementedError):
        safe_relative_path([b"a"], Path("/tmp/does-not-matter"))


def test_tracker_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        parse_compact_peers(b"\x7f\x00\x00\x01\x1a\xe1")


def test_peer_wire_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        Handshake(info_hash=ZERO_HASH, peer_id=generate_peer_id()).encode()
    with pytest.raises(NotImplementedError):
        Handshake.decode(bytes(HANDSHAKE_LEN))
    with pytest.raises(NotImplementedError):
        encode_message(Have(index=1))
    with pytest.raises(NotImplementedError):
        decode_message(b"\x04\x00\x00\x00\x01")
    with pytest.raises(NotImplementedError):
        unpack_bitfield(b"\x80", 1)
    with pytest.raises(NotImplementedError):
        pack_bitfield([0], 1)
    with pytest.raises(NotImplementedError):
        PeerState().apply(Have(index=0), piece_count=1)


def test_download_is_unbuilt() -> None:
    with pytest.raises(NotImplementedError):
        verify_piece(bytes(20), b"data")
    with pytest.raises(NotImplementedError):
        pick_piece({0, 1}, Counter({0: 3, 1: 1}))


def test_seeder_policy_is_unbuilt() -> None:
    candidate = UnchokeCandidate(
        peer=("127.0.0.1", 6819), interested=True, bytes_sent=0, connected_at=0.0
    )
    with pytest.raises(NotImplementedError):
        select_unchoked([candidate], 2)
    with pytest.raises(NotImplementedError):
        pick_optimistic([candidate], set())


async def test_piece_store_is_constructible_but_its_io_is_unbuilt(engine: Client) -> None:
    """The store has to be *constructible* on the scaffold — otherwise nothing
    downstream could be wired at all — while every method that touches a byte
    raises."""
    meta = Metainfo(
        name="t",
        announce=None,
        announce_list=(),
        piece_length=16,
        piece_hashes=(bytes(20),),
        files=(),
        total_length=16,
        info_hash=ZERO_HASH,
    )
    store = PieceStore(meta, engine.settings.download_dir, engine.pool)
    assert store.have_count == 0
    assert not store.has_piece(0)
    assert not store.is_complete
    assert store.missing == {0}

    with pytest.raises(NotImplementedError):
        await store.scan_existing()
    with pytest.raises(NotImplementedError):
        await store.read_block(0, 0, 16)
    with pytest.raises(NotImplementedError):
        await store.write_verified_piece(0, bytes(16))


def test_piece_size_is_wired_because_the_last_piece_is_short() -> None:
    """Not a vertical — just the off-by-one that makes the final piece fail its
    SHA-1 while every other piece passes, sending you to look for the bug in your
    hashing."""
    meta = Metainfo(
        name="t",
        announce=None,
        announce_list=(),
        piece_length=10,
        piece_hashes=(bytes(20), bytes(20), bytes(20)),
        files=(),
        total_length=25,
        info_hash=ZERO_HASH,
    )
    assert meta.piece_size(0) == 10
    assert meta.piece_size(2) == 5
    with pytest.raises(IndexError):
        meta.piece_size(3)


async def test_adding_a_torrent_trips_v2(http: httpx.AsyncClient) -> None:
    """`POST /torrents` is how you meet the first vertical. Delete this test once
    V2 parses — it should be the first thing that fails."""
    with pytest.raises(NotImplementedError):
        await http.post("/torrents", content=b"d4:infod4:name1:aee")


async def test_adding_a_magnet_trips_v2(http: httpx.AsyncClient) -> None:
    with pytest.raises(NotImplementedError):
        await http.post("/torrents/magnet", json={"uri": "magnet:?xt=urn:btih:" + "ab" * 20})
