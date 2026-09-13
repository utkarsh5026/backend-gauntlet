"""The two UDP planes and the backbone pump — **wired**, not a vertical.

One regional SFU holds two UDP sockets:

* **media** (`MEDIA_PORT`) — the participant-facing muxed STUN/RTP/RTCP socket.
  That plane *is* project 15; its pump mounts here during integration. The
  scaffold binds it so the port is held and advertised, and drains nothing.
* **cascade** (`CASCADE_PORT`) — the backbone. Relay copies from peer SFUs arrive
  here, and the pump below hands each to `CascadeMesh.on_relayed` (V2).

## Why a datagram endpoint and not a socket

Production runs on **uvloop** (`uvicorn[standard]`, `loop="auto"`); pytest runs
on the **stdlib** loop. uvloop does not implement the `loop.sock_*` family, so a
pump written the obvious way — a raw `socket` plus `await loop.sock_recvfrom()` —
passes every test and raises `NotImplementedError` on the first datagram in the
container. `loop.create_datagram_endpoint` with a `DatagramProtocol` works on
both loops; its callback feeds a **bounded** `asyncio.Queue`.

## Why the queue is bounded

`datagram_received` is a synchronous callback. It cannot await, so when the queue
is full it can drop or grow without limit — and growing without limit on a port
anyone can send to is a remote OOM. It drops and counts
(`conf_datagrams_dropped_total{plane,reason="inbox_full"}`). During the Hairpin,
that counter climbing on the cascade plane means the loop could not keep up with
three regions' relay copies: a CPython finding for the profile, not a network one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog

from .cascade import CascadeMesh
from .errors import MediaError
from .ids import Address
from .metrics import DATAGRAMS_DROPPED_TOTAL

__all__ = ["MAX_DATAGRAM", "UdpEndpoint", "open_udp_endpoint", "run_backbone"]

logger = structlog.get_logger(__name__)

MAX_DATAGRAM = 2048
"""Largest datagram either plane will keep: a ~1200-byte WebRTC MTU plus relay
framing, with room to spare. A guard on what we *queue*, not on what arrives."""


class _InboxProtocol(asyncio.DatagramProtocol):
    """Bridges the loop's datagram callbacks into a bounded queue."""

    def __init__(self, plane: str, inbox: asyncio.Queue[tuple[bytes, Address]]) -> None:
        self._plane = plane
        self._inbox = inbox
        self.dropped = 0

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        # IPv4 gives (host, port), IPv6 (host, port, flowinfo, scope_id). Normalised
        # here, once, so a peer lookup keyed on an address compares like with like.
        if len(data) > MAX_DATAGRAM:
            self._drop("oversized")
            return
        try:
            self._inbox.put_nowait((data, (str(addr[0]), int(addr[1]))))
        except asyncio.QueueFull:
            self._drop("inbox_full")

    def error_received(self, exc: Exception) -> None:
        # Usually an ICMP port-unreachable from a peer that went away. UDP promises
        # nothing, so this is a hint, not a failure to propagate.
        logger.debug("udp icmp error", plane=self._plane, error=str(exc))

    def _drop(self, reason: str) -> None:
        self.dropped += 1
        DATAGRAMS_DROPPED_TOTAL.labels(plane=self._plane, reason=reason).inc()


class UdpEndpoint:
    """A bound UDP socket, as a queue you receive from and a `send` you call."""

    __slots__ = ("_inbox", "_protocol", "_transport", "plane")

    def __init__(
        self,
        plane: str,
        transport: asyncio.DatagramTransport,
        protocol: _InboxProtocol,
        inbox: asyncio.Queue[tuple[bytes, Address]],
    ) -> None:
        self.plane = plane
        self._transport = transport
        self._protocol = protocol
        self._inbox = inbox

    @property
    def local_addr(self) -> Address:
        """The address actually bound — with port `0` configured, the only real port."""
        sockname: tuple[str, int] = self._transport.get_extra_info("sockname")
        return (str(sockname[0]), int(sockname[1]))

    @property
    def dropped(self) -> int:
        """Datagrams shed before any parser saw them (queue full or oversized)."""
        return self._protocol.dropped

    def send(self, payload: bytes, dst: Address) -> None:
        """Send one datagram. Not a coroutine: `sendto` on a datagram transport
        buffers and returns — UDP has no backpressure to await."""
        self._transport.sendto(payload, dst)

    async def receive(self) -> tuple[bytes, Address]:
        """Wait for the next datagram and its source."""
        return await self._inbox.get()

    def close(self) -> None:
        self._transport.close()


@asynccontextmanager
async def open_udp_endpoint(plane: str, port: int, inbox_size: int) -> AsyncGenerator[UdpEndpoint]:
    """Bind `0.0.0.0:port` for `plane`, closed on exit.

    `local_addr`, not `remote_addr`: a server socket accepting from every peer, so
    the kernel filters nothing by source — which is why V2 must authenticate the
    source of every relay copy itself.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[tuple[bytes, Address]] = asyncio.Queue(maxsize=inbox_size)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _InboxProtocol(plane, inbox),
        local_addr=("0.0.0.0", port),
    )
    endpoint = UdpEndpoint(plane, transport, protocol, inbox)
    logger.info("udp socket bound", plane=plane, port=endpoint.local_addr[1], inbox=inbox_size)
    try:
        yield endpoint
    finally:
        if endpoint.dropped:
            logger.warning("udp datagrams dropped", plane=plane, count=endpoint.dropped)
        endpoint.close()


async def run_backbone(endpoint: UdpEndpoint, cascade: CascadeMesh) -> None:
    """Pump relay copies off the backbone socket into V2 until cancelled.

    Cancellation, not a shutdown flag: asyncio cancels the `await` itself, so an
    idle backbone stops now rather than whenever the next relay copy happens to
    arrive. What is still queued is dropped — correct for media, whose playout
    deadline has passed by the time a stopping SFU could deliver it.

    A `MediaError` costs one datagram. A `NotImplementedError` from V2 is
    deliberately *not* caught: it ends this task loudly (see `main`), the HTTP
    server keeps serving, and `/readyz` reports the backbone down.
    """
    logger.info("backbone pump running (idles until a peer relays)")
    while True:
        datagram, source = await endpoint.receive()
        try:
            delivery = cascade.on_relayed(source, datagram)  # V2
        except MediaError as exc:
            # Debug, not warn: the rate is the signal and it lives in
            # conf_relay_dropped_total, not in a log anyone can flood.
            logger.debug("dropped relay datagram", error=str(exc))
            continue
        if delivery is None:
            continue
        # TODO(integration): hand `delivery.packet` to project 15's local fan-out
        # (a rewriter per local subscriber of `delivery.track`), sending on the
        # *media* endpoint, and observe `FORWARD_SECONDS` around it. Until then a
        # delivered relay copy stops here.
        logger.debug("relay copy delivered", room=delivery.track[0], publisher=delivery.track[1])
