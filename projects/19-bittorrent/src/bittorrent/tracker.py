"""V3 - Tracker announce: peer discovery over HTTP *and* UDP.
`src/bittorrent/tracker.py`.

A tracker answers one question: "who else has infohash X?" You announce, with
your progress, and it hands back a list of peers plus an `interval` telling you
how long to wait before asking again. Two transports, and you implement both:

* **HTTP** (BEP 3) - a `GET /announce?...` whose reply is a bencoded dict. The
  `peers` value is normally the **compact** form: 6 bytes per peer, a 4-byte
  IPv4 address and a 2-byte big-endian port, because a list of dicts costs about
  five times as much and a popular swarm answers thousands of these a second.
* **UDP** (BEP 15) - a small binary protocol. You first `connect` to get a
  connection-id that expires in about a minute (cheap anti-spoofing: a forged
  source IP cannot complete the round trip), then `announce`. Everything is
  big-endian, and every request is paired with its reply by a random
  transaction-id.

An announce is a periodic *side effect*, not a blocking step on the download
path: `started` when you join, a re-announce on the interval, `stopped` on a
clean exit. One dead tracker must not sink the download.

*Concept to internalize:* why compact encoding exists at swarm scale, and the
UDP connection-id handshake as a spoofing defence.

## The trap that eats a day: percent-encoding raw bytes

`info_hash` and `peer_id` are **20 raw bytes**, not text, and they go into a URL
query string. Handing them to anything that expects a `str` corrupts them:
`.decode()` fails or mangles, and a form-encoder that helpfully UTF-8s your
bytes turns 20 bytes into up to 40 and the tracker has never heard of the
resulting hash.

Python has the exactly-right tool, and its name says what it is for::

    from urllib.parse import quote_from_bytes
    quote_from_bytes(info_hash.raw, safe="")

`quote_from_bytes` takes `bytes` and percent-encodes byte-by-byte, which is what
BEP 3 specifies. `safe=""` matters: the default is `safe="/"`, and a `0x2F` byte
inside a hash would then pass through as a literal `/`.

This is also why the query string here is **built, not passed as `params=`**.
Every HTTP client's parameter encoder — httpx's included — is a text encoder;
this one parameter is not text.

## The uvloop trap, which this module is the reason for

Production runs on **uvloop** (uvicorn[standard], `loop="auto"`), while pytest
runs on the **stdlib** loop. uvloop does not implement the `loop.sock_*` family:
`loop.sock_recvfrom` raises `NotImplementedError` there while working perfectly
under pytest. A UDP tracker written on raw sockets therefore passes every test
you write and fails the moment it is deployed, with an exception that reads like
an unimplemented vertical.

So the datagram plumbing below is **wired for you**, on
`loop.create_datagram_endpoint` with a `DatagramProtocol`, bridged into a
**bounded** `asyncio.Queue`. Use `udp_channel`; do not reach for a socket. The
bound is not decoration: `datagram_received` is a callback the loop invokes with
no way to say "not now", so an unbounded queue is a UDP flood turned into
unbounded memory, and there is no backpressure to be had on a protocol with no
acknowledgements. Dropping is the only honest option, and the queue's `maxsize`
is where you choose it deliberately.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog

from .types import InfoHash, PeerId

__all__ = [
    "ACTION_ANNOUNCE",
    "ACTION_CONNECT",
    "ACTION_ERROR",
    "COMPACT_PEER_LEN",
    "UDP_INBOX_SIZE",
    "UDP_PROTOCOL_MAGIC",
    "AnnounceRequest",
    "AnnounceResponse",
    "Event",
    "PeerAddress",
    "UdpChannel",
    "announce_http",
    "announce_udp",
    "parse_compact_peers",
    "udp_channel",
]

logger = structlog.get_logger(__name__)

type PeerAddress = tuple[str, int]
"""A peer's `(host, port)`.

A plain tuple because that is the shape `asyncio.open_connection(*addr)` and
`create_datagram_endpoint(remote_addr=...)` already take — wrapping it in a
class would mean unwrapping it at every call site that matters."""

COMPACT_PEER_LEN = 6
"""Bytes per peer in the compact list: 4 for the IPv4 address, 2 for the
big-endian port. The IPv6 compact form (BEP 7) is 18."""

UDP_PROTOCOL_MAGIC = 0x41727101980
"""The fixed connection-id every BEP 15 `connect` request opens with. Not a
secret and not a checksum — it is a constant that makes a UDP tracker trivially
distinguishable from anything else that might arrive on the port."""

ACTION_CONNECT = 0
ACTION_ANNOUNCE = 1
ACTION_SCRAPE = 2
ACTION_ERROR = 3
"""BEP 15 action codes. A reply's action must match the request's, and
`ACTION_ERROR` carries a human-readable message instead of a peer list."""

UDP_INBOX_SIZE = 64
"""How many datagrams `udp_channel` will hold before dropping.

Sized for the protocol rather than for comfort: a tracker exchange is one reply
per request, so anything past a handful is either a retransmission or someone
spraying the port. See the module docstring on why dropping is the only honest
choice here."""


class Event(StrEnum):
    """The `event` a client reports on an announce.

    The values are the wire spellings for HTTP; UDP sends the *ordinal*
    (`none=0, completed=1, started=2, stopped=3`) instead, and note that the
    ordinals are not in the order you would guess — mapping them is part of V3
    and reading them off this enum's declaration order would be a bug.
    """

    NONE = ""
    """A periodic keep-alive re-announce. Sent as an empty value (or omitted)
    over HTTP."""

    STARTED = "started"
    """The first announce for this torrent. A tracker may treat anything else
    from an unknown peer as an error."""

    STOPPED = "stopped"
    """A clean exit. Sending this is what keeps you from lingering in the swarm
    as a peer nobody can reach — the criterion the graceful-shutdown item
    grades."""

    COMPLETED = "completed"
    """Sent once, on finishing the download. This is where a tracker's
    seeder/leecher counts come from."""


@dataclass(frozen=True, slots=True)
class AnnounceRequest:
    """What we tell the tracker about ourselves and our progress."""

    info_hash: InfoHash
    peer_id: PeerId

    port: int
    """The port *we* accept inbound peers on, so others can dial us back. The
    port actually bound — announcing a `0` from the config tells the swarm
    nothing can reach you."""

    uploaded: int
    downloaded: int

    left: int
    """Bytes still needed. `0` means we are a seed, and trackers use it to decide
    whom to hand us."""

    event: Event = Event.NONE


@dataclass(frozen=True, slots=True)
class AnnounceResponse:
    """What the tracker tells us back."""

    interval: int
    """Seconds to wait before re-announcing. Honor it — a client that ignores
    the interval is a client trackers ban."""

    peers: tuple[PeerAddress, ...]


class UdpChannel:
    """A bound UDP socket you can `send` on and `receive` from, as coroutines.

    Wired plumbing, not a vertical. It exists so the UDP tracker protocol is
    written against a queue instead of against a socket API that behaves
    differently on the two event loops this project runs on — see the module
    docstring on uvloop.
    """

    __slots__ = ("_inbox", "_transport", "dropped")

    def __init__(self, transport: asyncio.DatagramTransport, inbox: asyncio.Queue[bytes]) -> None:
        self._transport = transport
        self._inbox = inbox
        self.dropped = 0
        """Datagrams discarded because the inbox was full. Worth logging rather
        than hiding: a non-zero count during a normal announce means something
        is flooding the port, and a silent drop is indistinguishable from a
        tracker that never answered."""

    def send(self, payload: bytes) -> None:
        """Send one datagram.

        Not a coroutine, and that is not an oversight: `sendto` on a datagram
        transport buffers and returns immediately. There is no backpressure on
        UDP to await.
        """
        self._transport.sendto(payload)

    async def receive(self, timeout: float) -> bytes:
        """Wait for the next datagram, raising `TimeoutError` past `timeout`.

        A deadline rather than an open-ended wait because UDP has no failure
        signal at all: a tracker that is down, a firewall dropping your packets,
        and a tracker that is merely slow are the *same observation*, and only a
        timeout turns the first two into something you can retry past.
        """
        async with asyncio.timeout(timeout):
            return await self._inbox.get()


class _TrackerDatagramProtocol(asyncio.DatagramProtocol):
    """Bridges the loop's datagram callbacks into a bounded queue."""

    def __init__(self, inbox: asyncio.Queue[bytes]) -> None:
        self._inbox = inbox
        self.dropped = 0

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        """Called by the loop for each datagram. Synchronous, and cannot wait.

        This is the whole reason the queue is bounded and this method drops
        instead of blocking: it has no way to signal "not now" to the loop, so
        the choice is between discarding a datagram and growing the queue
        without limit. `put_nowait` under a `QueueFull` makes that choice
        explicit and countable.
        """
        try:
            self._inbox.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1

    def error_received(self, exc: Exception) -> None:
        """An ICMP error came back — typically port-unreachable from a tracker
        that is not listening. Informational: UDP gives no delivery guarantee, so
        this is a hint, not a failure to propagate."""
        logger.debug("udp error", error=str(exc))


@asynccontextmanager
async def udp_channel(remote: PeerAddress) -> AsyncGenerator[UdpChannel]:
    """Open a UDP channel to `remote`, closed on exit.

    Wired. Use it as::

        async with udp_channel(("tracker.example", 6969)) as chan:
            chan.send(connect_request)
            reply = await chan.receive(timeout=15)

    `create_datagram_endpoint` with `remote_addr` connects the socket, so
    `sendto` needs no address and the kernel filters out datagrams from anyone
    else — which removes a whole class of off-path spoofing before your code
    sees a byte of it.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=UDP_INBOX_SIZE)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _TrackerDatagramProtocol(inbox),
        remote_addr=remote,
    )
    channel = UdpChannel(transport, inbox)
    try:
        yield channel
    finally:
        channel.dropped = protocol.dropped
        if protocol.dropped:
            logger.warning("udp datagrams dropped", count=protocol.dropped, remote=remote)
        transport.close()


async def announce_http(
    client: httpx.AsyncClient,
    announce_url: str,
    request: AnnounceRequest,
    *,
    timeout: float,
) -> AnnounceResponse:
    """Announce over HTTP and parse the bencoded reply.

    TODO(V3): build the query string, GET it, decode the reply with
    `bencode.decode`, and turn `peers` into addresses.

    The parameters are `info_hash`, `peer_id`, `port`, `uploaded`, `downloaded`,
    `left`, `compact=1`, and `event` when it is not `Event.NONE`. The first two
    are raw bytes and must be percent-encoded with `quote_from_bytes(...,
    safe="")` into a string you assemble yourself — see the module docstring;
    passing them through `params=` is the failure this vertical is famous for.

    Handle the two shapes of failure separately, because they mean different
    things. A **transport** failure (`httpx.HTTPError`, a timeout) is the
    tracker being unreachable. A **protocol** failure is a 200 response whose
    bencoded dict contains `failure reason` — the tracker understood you and
    said no, and the message says why ("torrent not registered", "your IP is
    banned"). Both become `TrackerError`, and the message is what makes the
    difference actionable.

    `peers` arrives in either form and real trackers send both: the compact
    6-bytes-each string (hand it to `parse_compact_peers`) or a bencoded list of
    dicts with `ip` and `port` keys. `compact=1` is a *request*, not a promise,
    so handling only one of the two is a client that works against some trackers.

    Wrap the whole thing in `asyncio.timeout(timeout)` — or pass the deadline to
    httpx, and know which one you chose and why.
    """
    raise NotImplementedError("V3: HTTP announce - percent-encode raw bytes, GET, decode the reply")


async def announce_udp(
    tracker: PeerAddress,
    request: AnnounceRequest,
    *,
    timeout: float,
) -> AnnounceResponse:
    """Announce over the UDP tracker protocol (BEP 15).

    TODO(V3): the two-step exchange, over `udp_channel` (never a raw socket -
    see the module docstring).

    **connect** - send `UDP_PROTOCOL_MAGIC`, action `ACTION_CONNECT`, and a
    random 32-bit transaction-id (`secrets.randbits(32)`); read back 16 bytes of
    action, transaction-id, and a connection-id valid for about a minute.

    **announce** - send the connection-id, `ACTION_ANNOUNCE`, a fresh
    transaction-id, the infohash and peer-id as raw bytes, downloaded / left /
    uploaded, the event ordinal, an IP of 0, a key, `num_want` of -1, and your
    port. Read back the action, transaction-id, interval, leechers, seeders, and
    then a compact peer list.

    `struct` is the whole encoder. Always lead the format with `>` (or `!`):
    without an explicit byte-order character `struct` uses native order **and
    native alignment**, which silently inserts padding bytes and produces a
    frame no tracker will parse. The announce request is `>qii20s20sqqqiiiiH`
    if you lay it out in that order — count it against BEP 15 rather than
    trusting that sentence.

    Three things that are the actual work, not the framing:

    * **Match the transaction-id on every reply.** It is the only thing tying a
      datagram to your request, and on a connected UDP socket a late reply to a
      *previous* request is the realistic confusion, not an attack.
    * **Re-`connect` when the connection-id expires.** It is valid for ~60
      seconds, and a long-running client re-announcing on a 30-minute interval
      will always have a stale one. The tracker's answer is an `ACTION_ERROR`,
      which you handle by reconnecting once and retrying — not by giving up.
    * **Retry with a bounded backoff.** BEP 15 specifies `15 * 2**n` seconds for
      n in 0..8; a plain retry loop with a cap is a defensible simplification as
      long as it is bounded, because the criterion is that a dead tracker does
      not sink the download.

    An `ACTION_ERROR` reply carries a message and no peers: raise `TrackerError`
    with it rather than returning an empty peer list, which would look to the
    caller exactly like an empty swarm.
    """
    raise NotImplementedError("V3: UDP announce - connect then announce, big-endian (BEP 15)")


def parse_compact_peers(raw: bytes) -> tuple[PeerAddress, ...]:
    """Decode a compact peer list: 6 bytes per peer, 4-byte IPv4 + 2-byte
    big-endian port.

    TODO(V3): turn `raw` into addresses, raising `TrackerError` when its length
    is not a multiple of `COMPACT_PEER_LEN` — a partial trailing peer means the
    reply is malformed, and rounding it away hides a real bug.

    `struct.iter_unpack(">4sH", raw)` is the stdlib answer and it already raises
    on a length that is not a multiple of the record size, so the check you owe
    is turning that into your own error rather than letting a `struct.error`
    escape. `socket.inet_ntoa` (or `ipaddress.IPv4Address`) renders the 4 bytes;
    the `H` in the format has already handled the big-endian port, which is the
    step people hand-roll and get backwards.

    Worth deciding: a compact list can legitimately contain `0.0.0.0`, a port of
    0, or your own address. None of those are dialable, and filtering them here
    rather than discovering it in V4 saves a confusing round of connection
    failures. (The IPv6 form, BEP 7, is 18 bytes per peer - a clean stretch.)
    """
    raise NotImplementedError("V3: 6 bytes/peer -> (host, port), big-endian port")
