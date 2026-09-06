"""The SFU core — **wired** shared state that ties the verticals together.

It owns the room/peer graph and, per peer, the vertical state objects: an
`IceAgent` (V1), a per-subscriber `Rewriter` (V2) and `LayerSelector` (V3), and
a `BandwidthEstimator` (V4). The signaling handlers (`routes`) call the
`join_publisher` / `subscribe` ops to build the graph; the UDP `pump` calls the
`handle_*` media-plane ops for each datagram. Those media ops do the wired
lookup and fan-out and call into the vertical primitives — whose bodies are the
`NotImplementedError`s.

So this imports, starts and serves. With no clients it idles; the first STUN
check a real browser sends reaches `StunMessage.parse` and raises. That
traceback is your worklist.

## Where the mutex went

The Rust core wrapped its state in a `Mutex`, and the rule was "every method
takes the lock, does purely synchronous work, and returns a list of datagrams;
the pump sends them *after* dropping the lock — never hold it across `.await`".

There is no lock here, and that is not a simplification — it is the same rule
enforced by a different mechanism. These methods contain no `await`, so under
asyncio nothing can interleave with them: a coroutine yields only at an `await`,
and between two of them the event loop is not going anywhere. The mutex existed
to exclude *other threads*, and there are none.

What survives intact is the shape, and it is the part worth keeping: **do the
synchronous work, return the datagrams, let the caller send them.** The moment
one of these methods grows an `await` in the middle, the guarantee evaporates —
a second datagram can be dispatched into a half-updated routing table, and the
bug looks like a peer that intermittently receives someone else's media. If you
ever need I/O in here, return a description of it instead. That is what
`Datagram` is for.

(The other half of the trade: because it is all one thread, a slow method blocks
the whole media plane rather than one lock's worth of it. `handle_rtp` is on the
path for every packet times every subscriber, and it is the first thing to show
up in a `py-spy` profile of the boss fight.)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum

import structlog
from pydantic import BaseModel

from .bwe import BandwidthEstimator
from .config import Settings
from .errors import NotFoundError, RejectedError, TruncatedError
from .forward import Rewriter
from .ice import IceAgent, StunMessage, Username
from .metrics import (
    BYTES_FORWARDED_TOTAL,
    ESTIMATED_BITRATE_BPS,
    ICE_NOMINATED_TOTAL,
    KEYFRAME_REQUESTS_TOTAL,
    PEERS,
    ROOMS,
    RTP_DROPPED_TOTAL,
    RTP_FORWARDED_TOTAL,
    RTP_RECEIVED_TOTAL,
    STUN_MESSAGES_TOTAL,
)
from .simulcast import Decision, LayerSelector, SimulcastLayer
from .wire import RTP_MIN_HEADER, Address, RtpPacket

__all__ = ["Datagram", "PeerHandle", "PeerView", "Role", "RoomView", "Sfu", "Topology"]

logger = structlog.get_logger(__name__)

RTCP_RECEIVER_REPORT = 201
"""RTCP packet type for a Receiver Report — the loss-based BWE signal (V4)."""


class Role(StrEnum):
    PUBLISHER = "publisher"
    SUBSCRIBER = "subscriber"


@dataclass(frozen=True, slots=True)
class Datagram:
    """A datagram the pump should send on the media socket."""

    payload: bytes
    dst: Address


class PeerHandle(BaseModel):
    """The credentials + address signaling hands back so a client can ICE-connect."""

    peer_id: int
    ice_ufrag: str
    ice_pwd: str
    media_addr: str
    out_ssrc: int | None = None
    """For a subscriber: the stable SSRC it will receive on."""


class PeerView(BaseModel):
    """One peer in the topology snapshot."""

    id: int
    role: Role


class RoomView(BaseModel):
    room: str
    peers: list[PeerView]


class Topology(BaseModel):
    """What `GET /rooms` and `GET /status` publish about the live graph.

    Deliberately thin: peer ids and roles, no ICE credentials, no nominated
    addresses, no SSRCs. This endpoint is unauthenticated, and a topology view
    that leaks a `pwd` hands anyone who reads it the ability to complete ICE as
    that peer.
    """

    rooms: list[RoomView]


@dataclass(slots=True)
class Peer:
    """One connected peer and its per-role vertical state."""

    id: int
    room: str
    role: Role
    ice: IceAgent
    local_ufrag: str
    bwe: BandwidthEstimator

    layers: list[SimulcastLayer] = field(default_factory=list[SimulcastLayer])
    """Publisher: the simulcast layers it announced. Empty for a subscriber."""

    subscribes_to: int | None = None
    """Subscriber: the publisher it watches. `None` for a publisher."""

    rewriter: Rewriter | None = None
    selector: LayerSelector | None = None


class Sfu:
    """The shared SFU. One instance per process, held on the app state."""

    __slots__ = (
        "_by_addr",
        "_by_ssrc",
        "_by_ufrag",
        "_next_peer",
        "_peers",
        "_rooms",
        "_subscribers_of",
        "config",
    )

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._rooms: dict[str, list[int]] = {}
        self._peers: dict[int, Peer] = {}
        self._by_ufrag: dict[str, int] = {}
        """Local ICE ufrag -> peer, to route an inbound check before nomination."""
        self._by_addr: dict[Address, int] = {}
        """Nominated media source address -> peer, to route RTP/RTCP after ICE."""
        self._by_ssrc: dict[int, int] = {}
        """Publisher layer SSRC -> publisher, to find an inbound packet's origin."""
        self._subscribers_of: dict[int, list[int]] = {}
        """Publisher -> its subscribers.

        Maintained rather than derived, and that is a CPython decision. The Rust
        scanned every peer per packet to find the subscribers of one publisher;
        at 1500 packets/sec against a full room that is a linear scan a hundred
        thousand times a second, and an interpreted linear scan costs what a
        compiled one does not. The fan-out itself is unavoidably
        O(subscribers) — the *lookup* does not have to be.
        """
        self._next_peer = 1

    # ------------------------------------------------------------------ #
    # Signaling plane (wired) — build the room/peer graph.
    # ------------------------------------------------------------------ #

    def join_publisher(
        self,
        room: str,
        layers: list[SimulcastLayer],
        client_ufrag: str,
    ) -> PeerHandle:
        """Register a publisher announcing its simulcast layers."""
        self._ensure_room(room)
        local_ufrag, local_pwd = _generate_credentials()
        peer_id = self._next_peer
        self._next_peer += 1

        peer = Peer(
            id=peer_id,
            room=room,
            role=Role.PUBLISHER,
            ice=IceAgent(local_ufrag, local_pwd, client_ufrag),
            local_ufrag=local_ufrag,
            bwe=self._new_estimator(),
            layers=layers,
        )
        for layer in layers:
            self._by_ssrc[layer.ssrc] = peer_id
        self._subscribers_of.setdefault(peer_id, [])
        self._insert_peer(peer)

        logger.info("publisher joined", room=room, peer=peer_id, layers=len(layers))
        return PeerHandle(
            peer_id=peer_id,
            ice_ufrag=local_ufrag,
            ice_pwd=local_pwd,
            media_addr=_format_addr(self.config.media_addr),
        )

    def subscribe(self, room: str, publisher: int, client_ufrag: str) -> PeerHandle:
        """Register a subscriber watching `publisher`.

        Wires up its rewriter (V2) and layer selector (V3), and returns the
        stable SSRC it will receive on for the whole session — including across
        every layer switch, which is the point.
        """
        origin = self._peers.get(publisher)
        if origin is None or origin.role is not Role.PUBLISHER:
            raise NotFoundError(f"no publisher {publisher}")

        self._ensure_room(room)
        local_ufrag, local_pwd = _generate_credentials()
        out_ssrc = secrets.randbits(32)
        peer_id = self._next_peer
        self._next_peer += 1

        peer = Peer(
            id=peer_id,
            room=room,
            role=Role.SUBSCRIBER,
            ice=IceAgent(local_ufrag, local_pwd, client_ufrag),
            local_ufrag=local_ufrag,
            bwe=self._new_estimator(),
            subscribes_to=publisher,
            rewriter=Rewriter(out_ssrc),
            selector=LayerSelector(origin.layers),
        )
        self._insert_peer(peer)
        self._subscribers_of.setdefault(publisher, []).append(peer_id)

        logger.info("subscriber joined", room=room, peer=peer_id, publisher=publisher)
        return PeerHandle(
            peer_id=peer_id,
            ice_ufrag=local_ufrag,
            ice_pwd=local_pwd,
            media_addr=_format_addr(self.config.media_addr),
            out_ssrc=out_ssrc,
        )

    def topology(self) -> Topology:
        """A snapshot of the live graph for `GET /rooms` and `GET /status`."""
        return Topology(
            rooms=[
                RoomView(
                    room=room,
                    peers=[
                        PeerView(id=peer.id, role=peer.role)
                        for peer_id in peer_ids
                        if (peer := self._peers.get(peer_id)) is not None
                    ],
                )
                for room, peer_ids in self._rooms.items()
            ]
        )

    def _new_estimator(self) -> BandwidthEstimator:
        return BandwidthEstimator(
            self.config.start_bitrate,
            self.config.min_bitrate,
            self.config.max_bitrate,
        )

    def _ensure_room(self, room: str) -> None:
        """Create the room if new, and enforce both caps.

        Both limits are the "bounded everything" checklist item: an open
        signaling API means room names and join counts are attacker-chosen, and
        a room table with no cap is a dictionary an anonymous caller can grow
        until the process dies.
        """
        if room not in self._rooms:
            if len(self._rooms) >= self.config.max_rooms:
                raise RejectedError(f"max rooms ({self.config.max_rooms}) reached")
            self._rooms[room] = []
            ROOMS.set(len(self._rooms))
        if len(self._rooms[room]) >= self.config.max_peers_per_room:
            raise RejectedError(f"room full ({self.config.max_peers_per_room} peers)")

    def _insert_peer(self, peer: Peer) -> None:
        self._by_ufrag[peer.local_ufrag] = peer.id
        self._rooms.setdefault(peer.room, []).append(peer.id)
        self._peers[peer.id] = peer
        PEERS.labels(role=peer.role.value).inc()

    # ------------------------------------------------------------------ #
    # Media plane (wired dispatch -> vertical primitives).
    # ------------------------------------------------------------------ #

    def handle_stun(self, source: Address, datagram: bytes) -> list[Datagram]:
        """Handle an inbound STUN datagram (an ICE connectivity check). Reaches V1."""
        # The first todo on the media path: parsing the message (V1).
        message = StunMessage.parse(datagram)
        STUN_MESSAGES_TOTAL.labels(kind="request").inc()

        # Route the check to a peer by the local ufrag in its USERNAME.
        ufrag = _username_local_ufrag(message)
        peer_id = self._by_ufrag.get(ufrag) if ufrag is not None else None
        if peer_id is None:
            raise NotFoundError("no peer for STUN username")

        result = self._peers[peer_id].ice.handle(message, source)  # V1
        if result.nominated is not None:
            self._by_addr[result.nominated] = peer_id
            ICE_NOMINATED_TOTAL.inc()
            logger.info("ICE pair nominated", peer=peer_id, source=_format_addr(result.nominated))
        if result.response is None:
            return []
        return [Datagram(payload=result.response, dst=source)]

    def handle_rtp(self, source: Address, datagram: bytes) -> list[Datagram]:
        """Fan an inbound RTP packet out to the publisher's subscribers.

        Each subscriber gets it through its own selector (V3) and rewriter (V2).
        Returns the rewritten datagrams for the pump to send.
        """
        RTP_RECEIVED_TOTAL.inc()

        # The source must have completed ICE; an open port takes bytes from
        # anyone, and forwarding for an unauthenticated sender would make this
        # SFU a reflector.
        publisher_id = self._by_addr.get(source)
        if publisher_id is None:
            RTP_DROPPED_TOTAL.labels(reason="no_route").inc()
            return []
        if len(datagram) < RTP_MIN_HEADER:
            raise TruncatedError(need=RTP_MIN_HEADER, got=len(datagram))

        origin_ssrc = int.from_bytes(datagram[8:12], "big")
        # A crude keyframe heuristic for the switch boundary: H.264 NAL type 5,
        # assuming no CSRCs and no header extension. Real per-codec keyframe
        # detection (H.264 IDR vs VP8's `P` bit) is part of V3.
        is_keyframe = len(datagram) > RTP_MIN_HEADER and (datagram[RTP_MIN_HEADER] & 0x1F) == 5

        outgoing: list[Datagram] = []
        keyframe_owed = False
        for sub_id in self._subscribers_of.get(publisher_id, ()):
            peer = self._peers.get(sub_id)
            if peer is None or peer.selector is None or peer.rewriter is None:
                continue

            if peer.selector.on_packet(origin_ssrc, is_keyframe) is Decision.FORWARD:  # V3
                destination = peer.ice.peer
                if destination is not None:
                    # One mutable copy per subscriber: each gets its own
                    # sequence number, so they cannot share a buffer. This is
                    # the allocation the boss fight's CPU budget is spent on —
                    # see `wire.py`.
                    buf = bytearray(datagram)
                    peer.rewriter.rewrite(RtpPacket(buf))  # V2
                    RTP_FORWARDED_TOTAL.inc()
                    BYTES_FORWARDED_TOTAL.inc(len(buf))
                    outgoing.append(Datagram(payload=bytes(buf), dst=destination))
            else:
                peer.rewriter.skip()  # V2 — keeps the outbound line gapless
                RTP_DROPPED_TOTAL.labels(reason="not_selected").inc()

            keyframe_owed = keyframe_owed or peer.selector.wants_keyframe

        if keyframe_owed:
            publisher = self._peers.get(publisher_id)
            if publisher is not None and publisher.ice.peer is not None:
                KEYFRAME_REQUESTS_TOTAL.inc()
                logger.debug("would send PLI/FIR upstream", publisher=publisher_id)
                # TODO(protocols): build a real PLI (RTCP PSFB, fmt 1) here and
                # append it to `outgoing`. Until then an up-switch never gets
                # the keyframe it is waiting for, so V3 commits only against a
                # publisher that sends them on its own schedule.
        return outgoing

    def handle_rtcp(self, source: Address, datagram: bytes) -> list[Datagram]:
        """Handle inbound RTCP feedback. Reaches V4 on a receiver report."""
        if len(datagram) < 8:
            raise TruncatedError(need=8, got=len(datagram))
        peer_id = self._by_addr.get(source)
        if peer_id is None:
            return []

        # Receiver Report: the fraction-lost byte sits at offset 12, in the
        # first report block. A full compound parser (RR / NACK / TWCC / REMB)
        # is the reliability + observability horizontal work; this one byte is
        # enough to close the loop into the estimator.
        if datagram[1] == RTCP_RECEIVER_REPORT and len(datagram) >= 13:
            peer = self._peers.get(peer_id)
            if peer is not None:
                estimate = peer.bwe.on_loss(datagram[12] / 256.0)  # V4
                ESTIMATED_BITRATE_BPS.set(estimate)
                if peer.selector is not None:
                    peer.selector.set_budget(estimate)  # V3 consumes the estimate
        return []


def _generate_credentials() -> tuple[str, str]:
    """A fresh ICE ufrag + pwd (RFC 5245 wants >= 4 and >= 22 chars of ICE-safe text).

    `secrets`, not `random`. This is the one place in the project where the
    module choice is a security bug rather than a style preference: Python's
    `random` is a Mersenne Twister, and 624 observed outputs reveal its entire
    internal state — and an SFU hands out a ufrag to every caller who asks. With
    `random`, predicting the next peer's `pwd` is a solved exercise, and that
    `pwd` is the *only* thing standing between an attacker and a nominated ICE
    pair. Hex keeps every character inside the ICE-allowed set.
    """
    return secrets.token_hex(4), secrets.token_hex(16)


def _format_addr(address: Address) -> str:
    """Render an address for signaling and logs, bracketing IPv6 literals."""
    host, port = address
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _username_local_ufrag(message: StunMessage) -> str | None:
    """Pull the local (SFU-side) ufrag out of a STUN USERNAME (`<local>:<remote>`)."""
    for attribute in message.attributes:
        if isinstance(attribute, Username):
            return attribute.value.split(":", 1)[0]
    return None
