"""V2 — Inter-SFU cascade transport. `src/global_conferencing/cascade.py`.

This is the heart of "cascaded": the transport that makes an SFU a **peer of
another SFU**. A publisher's origin media lives in its home region (V1). A
subscriber in a *different* region must receive it — but the origin SFU must
**not** send one copy per remote subscriber across the ocean. It opens **one
relay leg per remote region with interest** and sends a **single** copy of each
forwarded stream down that leg; the remote SFU receives it and does the **local**
fan-out through project 15's per-subscriber rewriter. Inter-region cost becomes
`O(regions × layers)`, not `O(subscribers)` — the whole win.

The subtleties are all about being a relay **without a loop**. A relay copy
arriving from region `A` is fanned out to local subscribers but **never**
relayed onward — not back to `A`, not to a third region — or a 3-region mesh
forwards one packet in circles forever, amplifying itself into a backbone storm.
So a relayed datagram carries **provenance**, the SFU relays **only origin
media** and fans **relay media** out locally, and legs are **bounded**: a fixed
peer set, one leg each, torn down when the last remote subscriber leaves.

## The framing is yours, and it is attack surface

What a relay datagram looks like on the backbone — which bytes say which track,
which layer, which region it originated in — is V2's design, and it goes in
`docs/17-design.md`. Whatever you pick, `on_relayed` parses bytes that arrived
on an **open UDP port**. `struct.unpack_from` raises `struct.error` on a short
buffer, but plain slicing does not raise at all, so check `len(datagram)`
against the header size *first* and raise `TruncatedError`; the backbone pump
catches `MediaError` and moves on. `struct.pack` into a fixed header plus the
RTP bytes is the direct way to build one; for `O(regions)` copies the
allocation is not your bottleneck — the per-*subscriber* copies on the far side
are.

Scaffold state: construction, the peer lookup and the `/status` leg table are
wired, and the backbone UDP socket is bound and pumped in `transport.py`. The
first datagram that reaches the backbone port calls `on_relayed` and raises; the
pump task ends and `/readyz` turns 503 while `/healthz` stays green.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .config import PeerNode
from .ids import Address, LayerId, TrackKey
from .routing import LayerRouter

__all__ = ["CascadeConfig", "CascadeMesh", "LocalDelivery", "RelayDatagram", "RelayLeg"]


class RelayLeg(BaseModel):
    """One backbone relay leg — a live SFU↔SFU link for a peer region with interest.

    Exists only while that region has ≥1 subscriber for a stream relayed on it.
    The `/rooms` JSON the web playground renders is a list of these.
    """

    region: str
    """The peer region this leg is with."""

    remote_addr: str
    """The peer's backbone `host:port`."""

    tracks: int
    """Distinct origin tracks currently relayed on this leg."""


@dataclass(frozen=True, slots=True)
class RelayDatagram:
    """One datagram to send on the backbone socket."""

    dst: Address
    payload: bytes


@dataclass(frozen=True, slots=True)
class LocalDelivery:
    """What a valid relay copy becomes on the far side: the input to local fan-out.

    `packet` is the RTP datagram as the origin forwarded it, with the relay
    framing removed — what project 15's rewriter consumes for each local
    subscriber of `track`.
    """

    track: TrackKey
    packet: bytes


@dataclass(frozen=True, slots=True)
class CascadeConfig:
    region: str
    """This SFU's region — the loop guard's notion of "origin media is from here"."""

    peers: tuple[PeerNode, ...]
    """The peer SFUs legs can exist with."""

    max_legs: int
    """Cap on concurrently open relay legs (`MAX_RELAY_LINKS`)."""


class CascadeMesh:
    """The set of relay legs to peer regions, and the relay/fan-in decisions on them."""

    def __init__(self, config: CascadeConfig, router: LayerRouter) -> None:
        self.config = config
        self._router = router
        """V3, consulted per packet: a leg carries only the layers its region demands."""

        self._peers = {peer.region: peer for peer in config.peers}
        self._legs: dict[str, RelayLeg] = {}
        """Open legs keyed by peer region."""

        # TODO(V2): the rest of the leg state. Roughly: per leg, *which* tracks it
        # relays (a `set[TrackKey]` — `RelayLeg.tracks` is its length, not the set),
        # and per track, which regions want it, so `relay_out` is one dictionary
        # lookup per packet rather than a scan of every leg. And a way to recognise
        # a known peer by the *address* a datagram came from.

    @property
    def region(self) -> str:
        return self.config.region

    def legs(self) -> list[RelayLeg]:
        """A snapshot of the open relay legs (for `/rooms` and `/status`)."""
        return [self._legs[region] for region in sorted(self._legs)]

    def peer(self, region: str) -> PeerNode | None:
        """The peer SFU for `region`, if it is in the mesh."""
        return self._peers.get(region)

    # ---- V2 worklist: open legs · relay out · fan in (loop-free) -----------

    async def ensure_leg(self, region: str, track: TrackKey) -> RelayLeg:
        """Ensure a relay leg with `region` exists for `track`, and return it.

        TODO(V2): idempotent open-or-reuse. The first remote interest in a track
        opens the leg (bump `RELAY_LEGS{peer=region}`); later interest in the same
        track changes nothing on the backbone — that is the "doubling remote
        subscribers adds zero backbone packets" criterion, enforced here. Raise
        `NotFoundError` for a region not in `PEERS` and `ConflictError` past
        `config.max_legs`.

        Async because the origin has to *learn* that this region wants the track —
        the subscriber is here, the media is there. Whether that intent travels
        over HTTP, on the backbone itself, or is derived from V1's replicated
        membership is your call. Called from signaling when a subscriber's
        publisher lives in another region.
        """
        raise NotImplementedError(
            "V2: open-or-reuse the relay leg to this region, enforce MAX_RELAY_LINKS"
        )

    async def release_leg(self, region: str, track: TrackKey) -> None:
        """Drop interest in `track` on the leg with `region`.

        TODO(V2): close the leg when its last track goes. Idempotent — releasing an
        already-closed leg is harmless, because a subscriber's leave and a region's
        membership retirement can both arrive for the same track.
        """
        raise NotImplementedError("V2: decrement leg interest, close the leg when it reaches zero")

    def relay_out(self, track: TrackKey, layer: LayerId, packet: bytes) -> list[RelayDatagram]:
        """Relay **one** copy of an origin packet to each remote region that wants it.

        TODO(V2): one `RelayDatagram` per leg carrying `track` *and* whose V3 set
        carries `layer` (`self._router.leg_carries`) — regardless of how many
        subscribers that region has. Frame it with provenance so the far side can
        tell it is a relay copy. **Loop guard:** this is only ever called with
        media produced in this region; a packet that arrived as a relay copy must
        never come back through here. Bump `RELAY_COPIES_OUT_TOTAL` and
        `RELAY_BYTES_OUT_TOTAL` per region pair.

        Synchronous on purpose: it runs per origin packet, and the returned
        datagrams are sent with a non-blocking `sendto`.
        """
        raise NotImplementedError(
            "V2: send one relay copy per interested remote region (origin media only)"
        )

    def on_relayed(self, source: Address, datagram: bytes) -> LocalDelivery | None:
        """A datagram arrived on the backbone socket from `source`.

        TODO(V2): authenticate the source (a known peer's backbone address, else
        raise `UnknownPeerError`), bounds-check and strip the framing (else
        `TruncatedError`), and return the `LocalDelivery` for local fan-out.
        **Loop guard:** a relay copy is delivered locally and **never** re-relayed
        — there is no code path from here to `relay_out`. Return `None` to drop a
        copy that would close a loop (bump `RELAY_DROPPED_TOTAL{reason="loop"}`).
        Bump `RELAY_COPIES_IN_TOTAL` for each accepted copy.
        """
        raise NotImplementedError(
            "V2: authenticate peer, strip framing, fan out locally, never re-relay (loop-free)"
        )

    async def close_all(self) -> None:
        """Tear down every relay leg on graceful shutdown.

        TODO(V2): no half-open legs — a peer still relaying to a stopped SFU sends
        media at a port nobody drains. Tell each peer (however `ensure_leg` told
        it), then clear the table. Called from the lifespan before the backbone
        socket closes.
        """
        raise NotImplementedError("V2: tear down every relay leg cleanly (graceful shutdown)")
