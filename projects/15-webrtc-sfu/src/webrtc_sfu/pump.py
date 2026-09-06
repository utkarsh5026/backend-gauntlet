"""The UDP media-plane pump — **wired**, not a vertical.

One muxed UDP socket carries STUN, RTP and RTCP together (WebRTC bundles them).
The pump does the only thing the media plane needs at the top level: take the
next datagram, `classify` it (RFC 7983 first-byte demux), hand it to the
matching `Sfu` dispatch method, and send back whatever datagrams that produced.
Every interesting decision happens inside those dispatch methods and the
vertical primitives they call.

Media-plane errors are **dropped, not fatal**. A malformed datagram from an open
UDP port costs one packet, never the loop — that is the one `except MediaError`
below, and it is the whole reason `errors.py` gives every bad-datagram failure a
common base class. A `NotImplementedError` from a vertical is deliberately *not*
caught: it ends the pump task and gets logged loudly, exactly like the Rust
scaffold's `todo!()` panic, while the signaling and admin HTTP server keeps
serving. That is your worklist, and it should be loud.

## Why a datagram endpoint and not a socket

This is the single most important structural decision in the file, and it is
invisible until it bites.

Production runs on **uvloop** (`uvicorn[standard]`, `loop="auto"`). pytest runs
on the **stdlib** event loop. uvloop does not implement the `loop.sock_*`
family, so a pump written the obvious way — a raw `socket` plus
`await loop.sock_recvfrom(...)` — passes every test you write and raises
`NotImplementedError` the moment it runs in the container. Two loops, one of
them only ever exercised in production, is exactly the shape of bug that ships.

So: `loop.create_datagram_endpoint` with a `DatagramProtocol`, which both loops
implement, bridged into a **bounded** `asyncio.Queue`. That is also why the
SPEC's Definition of done asks you to boot the container rather than trusting
`make verify`.

## Why the queue is bounded, and what the bound means

`datagram_received` is a *callback* the loop invokes. It is synchronous and it
cannot await, so when the queue is full it has exactly two options: discard the
datagram, or grow without limit. Growing without limit on an open UDP port is a
remote OOM with no authentication in front of it, so it drops — and counts what
it dropped, because a silent drop is indistinguishable from a peer that never
sent anything.

The bound (`MEDIA_INBOX`) is therefore real backpressure and a real tuning knob:
too small and a burst of ICE checks from fifty subscribers joining at once gets
shed; too large and you have merely moved the latency into a queue, which on a
media path is worse than dropping — a packet that waits 400 ms to be forwarded
is a packet the subscriber's jitter buffer has already given up on.

`sfu_rtp_dropped_total{reason="inbox_full"}` climbing during the Crowded Room is
a *CPython* finding, not a network one: it means the loop could not drain the
queue fast enough, and the profile will say whether that was the fan-out's
per-subscriber `bytearray` copies, the GC, or something blocking the loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog

from .errors import MediaError
from .metrics import FORWARDING_SECONDS, RTP_DROPPED_TOTAL
from .sfu import Datagram, Sfu
from .wire import Address, PacketKind, classify

__all__ = ["MAX_DATAGRAM", "MediaSocket", "open_media_socket", "run"]

logger = structlog.get_logger(__name__)

MAX_DATAGRAM = 2048
"""Largest datagram we are willing to look at.

Above a typical 1200-byte WebRTC MTU with room for a jumbo-ish RTCP compound.
The loop hands us whatever arrived, so this is a guard on what we *keep*, not on
what the kernel accepts.
"""


class _MediaDatagramProtocol(asyncio.DatagramProtocol):
    """Bridges the loop's datagram callbacks into a bounded queue."""

    def __init__(self, inbox: asyncio.Queue[tuple[bytes, Address]]) -> None:
        self._inbox = inbox
        self.dropped = 0

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        """Called by the loop for each datagram. Synchronous; cannot wait.

        `addr` is a 2-tuple for IPv4 and a 4-tuple for IPv6 (host, port,
        flowinfo, scope_id). It is normalised to `(host, port)` here, at the one
        boundary where both shapes exist, so nothing downstream — the nomination
        table, XOR-MAPPED-ADDRESS, the route lookup — has to know there were
        ever two. See `wire.Address` for what that costs.
        """
        if len(data) > MAX_DATAGRAM:
            self.dropped += 1
            RTP_DROPPED_TOTAL.labels(reason="malformed").inc()
            return
        source: Address = (str(addr[0]), int(addr[1]))
        try:
            self._inbox.put_nowait((data, source))
        except asyncio.QueueFull:
            self.dropped += 1
            RTP_DROPPED_TOTAL.labels(reason="inbox_full").inc()

    def error_received(self, exc: Exception) -> None:
        """An ICMP error came back — usually port-unreachable from a peer that
        went away. Informational: UDP promises nothing, so this is a hint rather
        than a failure to propagate."""
        logger.debug("media socket icmp error", error=str(exc))


class MediaSocket:
    """The bound muxed UDP socket, as a queue you receive from and a send you call.

    Wired plumbing. It exists so the pump is written against a queue rather than
    against a socket API that behaves differently on the two event loops this
    project runs on — see the module docstring.
    """

    __slots__ = ("_inbox", "_protocol", "_transport")

    def __init__(
        self,
        transport: asyncio.DatagramTransport,
        protocol: _MediaDatagramProtocol,
        inbox: asyncio.Queue[tuple[bytes, Address]],
    ) -> None:
        self._transport = transport
        self._protocol = protocol
        self._inbox = inbox

    @property
    def local_addr(self) -> Address:
        """The address actually bound.

        Worth asking rather than assuming: with `MEDIA_PORT=0` the configured
        port and the bound port are different numbers, and the bound one is the
        only one a client can send to.
        """
        sockname: tuple[str, int] = self._transport.get_extra_info("sockname")
        return (str(sockname[0]), int(sockname[1]))

    @property
    def dropped(self) -> int:
        """Datagrams discarded because the inbox was full or the datagram was huge."""
        return self._protocol.dropped

    def send(self, payload: bytes, dst: Address) -> None:
        """Send one datagram.

        Not a coroutine, and that is not an oversight: `sendto` on a datagram
        transport buffers and returns immediately. There is no backpressure on
        UDP to await, which is also why the fan-out cost is CPU rather than
        wait — 50 subscribers is 50 synchronous `sendto` calls in a row on the
        one thread that also runs the HTTP server.
        """
        self._transport.sendto(payload, dst)

    async def receive(self) -> tuple[bytes, Address]:
        """Wait for the next datagram and its source address."""
        return await self._inbox.get()

    def close(self) -> None:
        self._transport.close()


@asynccontextmanager
async def open_media_socket(port: int, inbox_size: int) -> AsyncGenerator[MediaSocket]:
    """Bind the muxed media socket on `0.0.0.0:port`, closed on exit.

    Bound with `local_addr` rather than `remote_addr`: this is a server socket
    that must accept datagrams from every peer in every room, so unlike a client
    channel it cannot let the kernel filter by source. That is precisely why
    every parser downstream has to be total on garbage — nothing between the
    wire and `StunMessage.parse` is filtering anything.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[tuple[bytes, Address]] = asyncio.Queue(maxsize=inbox_size)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _MediaDatagramProtocol(inbox),
        local_addr=("0.0.0.0", port),
    )
    socket = MediaSocket(transport, protocol, inbox)
    logger.info(
        "media udp socket bound (STUN/RTP/RTCP muxed)",
        port=socket.local_addr[1],
        inbox=inbox_size,
    )
    try:
        yield socket
    finally:
        if socket.dropped:
            logger.warning("media datagrams dropped", count=socket.dropped)
        socket.close()


async def run(socket: MediaSocket, sfu: Sfu) -> None:
    """Run the media pump until cancelled.

    Cancellation, not a shutdown flag. The Rust selected over a watch channel
    because it had to poll two futures; asyncio cancels the `await` itself, so
    the loop stops *inside* the receive rather than after the next datagram
    happens to arrive — on an idle room, the difference between shutting down
    now and shutting down whenever someone next sends a packet.

    What that drops is anything still queued in the inbox, and for media that is
    the right trade: a packet held at SIGTERM is a packet whose playout deadline
    passes before it could be delivered anyway.
    """
    logger.info("media pump running (idles until traffic)")
    while True:
        datagram, source = await socket.receive()
        started = time.perf_counter()
        kind = classify(datagram)
        try:
            outgoing: list[Datagram] = []
            match kind:
                case PacketKind.STUN:
                    outgoing = sfu.handle_stun(source, datagram)  # V1
                case PacketKind.RTP:
                    outgoing = sfu.handle_rtp(source, datagram)  # V2 + V3
                case PacketKind.RTCP:
                    outgoing = sfu.handle_rtcp(source, datagram)  # V4 + V2
                case PacketKind.UNKNOWN:
                    # DTLS, ZRTP, a TURN channel, garbage. Dropped in silence:
                    # this is the common case on a public port and logging it
                    # would hand anyone with a packet generator your log volume.
                    continue
        except MediaError as exc:
            # One bad datagram, bounded and non-fatal. Debug rather than warn
            # for the same reason — the rate is the signal, and the rate lives
            # in `sfu_rtp_dropped_total`, not in the log.
            logger.debug("dropped datagram", error=str(exc), kind=str(kind))
            continue

        for out in outgoing:
            socket.send(out.payload, out.dst)
        if kind is PacketKind.RTP:
            # Ingress packet to egress sendto — the boss fight's p99 <= 10 ms.
            FORWARDING_SECONDS.observe(time.perf_counter() - started)
