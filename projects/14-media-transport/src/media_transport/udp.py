"""The UDP media plane — **wired**, not a vertical.

One socket carries this transport's RTP and its RTCP feedback. The sender emits
RTP on it and reads feedback from it; the receiver does the reverse. Everything
interesting happens in `session.py` and the vertical primitives it calls — this
module exists only so those are written against a queue rather than a socket.

Media-plane errors are **dropped, not fatal**. A malformed datagram from an open
UDP port costs one packet, never the loop — that is the single
`except MediaError` in `session.py`, and it is the whole reason `errors.py`
gives every bad-datagram failure a common base class. A `NotImplementedError`
from a vertical is deliberately *not* caught: it ends the session task and gets
logged loudly, exactly like the Rust scaffold's `todo!()` panic, while the admin
HTTP server keeps serving. That is your worklist, and it should be loud.

## Why a datagram endpoint and not a raw socket

This is the single most important structural decision in the file, and it is
invisible until it bites.

Production runs on **uvloop** (`uvicorn[standard]`, `loop="auto"`). pytest runs
on the **stdlib** event loop. uvloop does not implement the `loop.sock_*`
family, so a receive loop written the obvious way — a `socket.socket` plus
`await loop.sock_recvfrom(...)` — passes every test you will ever write and
raises `NotImplementedError` the moment it runs in the container. Two loops, one
of them exercised only in production, is precisely the shape of bug that ships.

So: `loop.create_datagram_endpoint` with a `DatagramProtocol`, which both loops
implement, bridged into a **bounded** `asyncio.Queue`. That is also why the
SPEC's Definition of done asks you to boot the container rather than trusting
`make verify`.

## Why the queue is bounded, and what the bound means

`datagram_received` is a *callback* the loop invokes. It is synchronous and it
cannot await, so when the queue is full it has exactly two options: discard the
datagram, or grow without limit. Growing without limit on an open UDP port is a
remote OOM with no authentication in front of it — so it drops, and it counts
what it dropped, because a silent drop is indistinguishable from a peer that
never sent anything.

The bound (`RTP_INBOX`) is therefore real backpressure and a real tuning knob.
Too small and a burst — a keyframe fragmented into thirty packets, or a NACK
answered with seventeen retransmits at once — gets shed. Too large and you have
merely moved the latency into a queue, which on a media path is worse than
dropping: a packet that waits 400 ms to be jitter-buffered is a packet whose
playout deadline has already passed, so you paid to keep it and then dropped it
anyway one layer further in.

`media_transport_datagrams_dropped_total{reason="inbox_full"}` climbing during
the Lossy Mile is a **CPython finding**, not a network one. It means the loop
could not drain fast enough, and the profile will say whether that was the
per-packet allocation in `RtpPacket.parse`, the GC, or something blocking the
loop. That is exactly the kind of gap the SPEC asks you to record rather than
design around.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog

from .metrics import DATAGRAMS_DROPPED_TOTAL

__all__ = ["MAX_DATAGRAM", "Address", "MediaSocket", "open_media_socket"]

logger = structlog.get_logger(__name__)

type Address = tuple[str, int]
"""A peer address, always `(host, port)`.

Normalised at the one boundary where two shapes exist — the loop hands IPv4 a
2-tuple and IPv6 a 4-tuple `(host, port, flowinfo, scope_id)` — so nothing
downstream has to know there were ever two."""

MAX_DATAGRAM = 2048
"""Largest datagram this transport will keep.

Comfortably above the 1200-byte MTU the packetizer targets, with room for a
jumbo-ish RTCP compound. The loop hands us whatever arrived, so this is a guard
on what we *keep*, not on what the kernel accepts — an attacker can still make
the kernel receive a 64 KB datagram, they just cannot make us hold it."""


class _MediaDatagramProtocol(asyncio.DatagramProtocol):
    """Bridges the loop's datagram callbacks into a bounded queue."""

    def __init__(self, inbox: asyncio.Queue[tuple[bytes, Address]]) -> None:
        self._inbox = inbox
        self.dropped = 0

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        """Called by the loop for each datagram. Synchronous; cannot wait."""
        if len(data) > MAX_DATAGRAM:
            self.dropped += 1
            DATAGRAMS_DROPPED_TOTAL.labels(reason="oversized").inc()
            return
        source: Address = (str(addr[0]), int(addr[1]))
        try:
            self._inbox.put_nowait((data, source))
        except asyncio.QueueFull:
            self.dropped += 1
            DATAGRAMS_DROPPED_TOTAL.labels(reason="inbox_full").inc()

    def error_received(self, exc: Exception) -> None:
        """An ICMP error came back — usually port-unreachable from a peer that
        is not there yet, which is the normal state of affairs when you start
        the sender before the receiver. UDP promises nothing, so this is a hint
        rather than a failure to propagate."""
        logger.debug("media socket icmp error", error=str(exc))


class MediaSocket:
    """The bound UDP socket, as a queue you receive from and a send you call.

    Wired plumbing. It exists so the session loops are written against a queue
    rather than against a socket API that behaves differently on the two event
    loops this project runs on — see the module docstring.
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

        Worth asking rather than assuming: with `RTP_PORT=0` the configured port
        and the bound port are different numbers, and the bound one is the only
        one a peer can send to.
        """
        sockname: tuple[str, int] = self._transport.get_extra_info("sockname")
        return (str(sockname[0]), int(sockname[1]))

    @property
    def dropped(self) -> int:
        """Datagrams discarded because the inbox was full or the datagram huge."""
        return self._protocol.dropped

    def send(self, payload: bytes, dst: Address) -> None:
        """Send one datagram.

        Not a coroutine, and that is not an oversight: `sendto` on a datagram
        transport buffers and returns immediately. There is no backpressure on
        UDP to await — which is exactly why V4 exists, because if the transport
        will not push back, the pacing has to come from you.
        """
        self._transport.sendto(payload, dst)

    async def receive(self) -> tuple[bytes, Address]:
        """Wait for the next datagram and its source address."""
        return await self._inbox.get()

    def close(self) -> None:
        self._transport.close()


@asynccontextmanager
async def open_media_socket(port: int, inbox_size: int) -> AsyncGenerator[MediaSocket]:
    """Bind the media socket on `0.0.0.0:port`, closed on exit.

    Bound with `local_addr` rather than `remote_addr` even for the sender, which
    does have a fixed peer. Connecting the socket would let the kernel filter by
    source — tempting — but it also means an ICMP port-unreachable from a peer
    that has not started yet becomes an exception on the *send* path rather than
    an `error_received` callback, and RTCP feedback from a different port than
    the one you send to (which real stacks do) would be silently discarded by
    the kernel before you ever see it.

    That decision is why every parser downstream has to be total on garbage:
    nothing between the wire and `RtpPacket.parse` is filtering anything, and
    the SSRC validation in the security checklist is the *application's* filter
    standing in for the one the kernel is not doing.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[tuple[bytes, Address]] = asyncio.Queue(maxsize=inbox_size)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _MediaDatagramProtocol(inbox),
        local_addr=("0.0.0.0", port),
    )
    socket = MediaSocket(transport, protocol, inbox)
    logger.info("rtp/udp socket bound", port=socket.local_addr[1], inbox=inbox_size)
    try:
        yield socket
    finally:
        if socket.dropped:
            logger.warning("media datagrams dropped", count=socket.dropped)
        socket.close()
