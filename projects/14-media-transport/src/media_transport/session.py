"""The transport sessions — **wired** loops that tie the verticals together.

Two roles run one UDP socket in opposite directions:

* `run_sender`: pull a frame → **packetize** (V1) → **pace** (V4) → send; on
  RTCP feedback, **retransmit** NACK'd packets (V3) and update the estimate (V4).
* `run_receiver`: receive → **parse** (V1) → **jitter buffer** (V2); on a
  playout tick, release and **depacketize** (V1) complete frames; periodically
  **NACK** the gaps (V3).

The loops, the timers, the socket I/O and the volume metrics are done. The calls
they make into `rtp`/`jitter`/`rtcp`/`congestion` are the `NotImplementedError`s.
So the scaffold runs: as `ROLE=receiver` it binds and idles until a datagram
arrives, then reaches `RtpPacket.parse`; as `ROLE=sender` it produces a
synthetic frame and reaches `Packetizer.packetize`. Those raises are your
worklist.

## Structured concurrency, not `select!`

The Rust ran one loop with `tokio::select!` over three futures, because that is
how you await several things in one task. Python has `asyncio.TaskGroup`: each
concern gets its own coroutine, they run concurrently, and the `async with`
block is the join point. If one raises, the group cancels its siblings and
re-raises — which is exactly the `todo!()` semantics we want, one vertical
raising takes the whole session down loudly while the admin server keeps
serving. If the *group* is cancelled (shutdown), every child is cancelled inside
its `await`, so an idle receiver stops immediately rather than whenever the next
datagram happens to arrive.

## Why the shared state needs no lock

`run_receiver` mutates one `JitterBuffer` from three concurrent tasks — the
receive loop inserts, the playout tick pops, the feedback tick reads gaps — and
there is no lock anywhere. That is correct, and it is worth understanding rather
than copying.

asyncio is single-threaded and cooperative: a coroutine runs until it hits an
`await`, and only then can another one run. None of the three critical sections
here contains an `await` — `buffer.insert(...)` is a plain synchronous call —
so each runs to completion before any other task is scheduled. There is no
interleaving to protect against.

The moment that stops being true is the moment you put an `await` in the middle
of a read-modify-write, and the bug it produces is a torn update that only
appears under load. If you find yourself wanting one, that is the signal to
reach for `asyncio.Lock` — not before.
"""

from __future__ import annotations

import asyncio
import random
import time

import structlog

from .config import Settings
from .congestion import CongestionController
from .errors import MediaError
from .jitter import JitterBuffer
from .media import SyntheticSource
from .metrics import (
    BUFFER_DEPTH,
    BYTES_RECEIVED_TOTAL,
    BYTES_SENT_TOTAL,
    NACKS_TOTAL,
    PACKETS_LOST_TOTAL,
    PACKETS_RECEIVED_TOTAL,
    PACKETS_SENT_TOTAL,
    RETRANSMITS_TOTAL,
    TARGET_BITRATE_BPS,
)
from .rtcp import Bye, Nack, ReceiverReport, RetransmitCache, parse_compound, serialize
from .rtp import H264_CLOCK_RATE, SEQ_MASK, Packetizer, RtpPacket, depacketize
from .udp import Address, MediaSocket

__all__ = ["PLAYOUT_TICK", "FEEDBACK_INTERVAL", "run_receiver", "run_sender"]

logger = structlog.get_logger(__name__)

PLAYOUT_TICK = 0.010
"""How often the receiver checks for playable frames, in seconds.

10 ms against a 100 ms playout target: fine enough that the tick itself adds at
most a tenth of the budget. Making it much finer buys nothing and costs a wakeup
— on a single-threaded loop that also runs the receive path, the tick rate is a
CPU decision, not a latency one."""

FEEDBACK_INTERVAL = 0.200
"""How often the receiver emits RTCP feedback (NACK / receiver report).

RFC 3550 caps RTCP at ~5% of session bandwidth, and 200 ms is the usual
compromise: fast enough that a NACK'd packet can still be retransmitted inside a
100 ms playout budget on a low-RTT path, slow enough not to be its own traffic
problem. It is also a criterion — "rate-limited so a burst of loss doesn't melt
the back-channel" — so if you change it, change it in the design doc too."""


async def run_sender(socket: MediaSocket, config: Settings, remote: Address) -> None:
    """Run the sender side until cancelled.

    Two concurrent concerns: producing and pacing media, and consuming the
    receiver's feedback. They share the congestion controller and the retransmit
    cache — see the module docstring on why that needs no lock.
    """
    # RFC 3550 wants a random SSRC and a randomised starting sequence number, so
    # that a mixer or relay can never assume a stream starts at zero — and so
    # that V2's wraparound handling is exercised in the first few minutes of a
    # run rather than never.
    ssrc = random.getrandbits(32)
    packetizer = Packetizer(ssrc, config.payload_type, config.mtu, random.getrandbits(16))
    controller = CongestionController(config.start_bitrate, config.min_bitrate, config.max_bitrate)
    cache = RetransmitCache(config.rtx_cache_packets)
    source = SyntheticSource(config.fps, config.gop)

    TARGET_BITRATE_BPS.set(controller.target_bitrate)
    logger.info("sender started", remote=f"{remote[0]}:{remote[1]}", ssrc=ssrc)

    async def produce() -> None:
        """Frame → packets → paced onto the wire."""
        while True:
            frame = await source.next_frame(controller.target_bitrate)
            packets = packetizer.packetize(frame.data, frame.rtp_timestamp)  # V1
            for packet in packets:
                datagram = packet.to_bytes()  # V1
                # V4 paces here. `delay_before` returns seconds rather than a
                # boolean gate precisely so this is an await and not a spin —
                # see congestion.py on why that shape changed from the Rust.
                delay = controller.delay_before(time.monotonic(), len(datagram))
                if delay > 0:
                    await asyncio.sleep(delay)
                socket.send(datagram, remote)
                controller.on_sent(len(datagram))  # V4
                cache.record(packet)  # V3
                PACKETS_SENT_TOTAL.labels(kind="original").inc()
                BYTES_SENT_TOTAL.inc(len(datagram))

    async def consume_feedback() -> None:
        """RTCP from the receiver: NACKs to answer, reports to learn from."""
        while True:
            datagram, _source = await socket.receive()
            BYTES_RECEIVED_TOTAL.inc(len(datagram))
            try:
                feedback = parse_compound(datagram)  # V3
            except MediaError as exc:
                # One bad datagram, bounded and non-fatal. Debug rather than
                # warn: the *rate* is the signal, and the rate lives in
                # `media_transport_datagrams_dropped_total`, not in the log.
                logger.debug("dropped feedback datagram", error=str(exc))
                continue

            for packet in feedback:
                match packet:
                    case Nack():
                        NACKS_TOTAL.labels(dir="received").inc()
                        for sequence in packet.lost:
                            # V3 decides what is still worth resending: a
                            # cache miss here is an eviction, but the
                            # *deadline* check is yours to add — that is the
                            # difference between deadline-aware and merely
                            # forgetful.
                            held = cache.get(sequence & SEQ_MASK)  # V3
                            if held is None:
                                continue
                            socket.send(held.to_bytes(), remote)
                            RETRANSMITS_TOTAL.inc()
                            PACKETS_SENT_TOTAL.labels(kind="retransmit").inc()
                    case ReceiverReport():
                        # The wire carries an 8-bit numerator over 256 and ticks
                        # of the media clock; the controller takes a fraction
                        # and seconds. Converting here, once, is what lets
                        # congestion.py never wonder which unit it holds.
                        TARGET_BITRATE_BPS.set(
                            controller.on_receiver_report(  # V4
                                packet.fraction_lost / 256.0,
                                packet.jitter / H264_CLOCK_RATE,
                            )
                        )
                    case Bye():
                        logger.info("peer sent RTCP BYE", ssrcs=list(packet.ssrcs))

    # TODO(protocol / graceful shutdown): on cancellation a sender should emit
    # an RTCP BYE so the peer learns the source is gone rather than waiting out
    # a timeout. The place for it is a `finally` around this block — but note
    # that the group is being cancelled, so anything you await there needs
    # `asyncio.shield` or it is cancelled too. That subtlety is the reason this
    # is a checklist item and not a freebie.
    async with asyncio.TaskGroup() as group:
        group.create_task(produce(), name="sender-produce")
        group.create_task(consume_feedback(), name="sender-feedback")


async def run_receiver(socket: MediaSocket, config: Settings) -> None:
    """Run the receiver side until cancelled.

    Three concurrent concerns over one jitter buffer: admit arrivals, release
    frames on a playout tick, and emit feedback on a slower tick.
    """
    self_ssrc = random.getrandbits(32)
    buffer = JitterBuffer(config.target_playout, H264_CLOCK_RATE, config.jitter_capacity)
    # The stream being received, learned from the first packet. The security
    # checklist's source-validation item is about what happens to the *second*
    # SSRC that shows up — right now it simply replaces this one, which is the
    # behaviour a stray or spoofed source needs you to change.
    media_ssrc = 0
    peer: Address | None = None

    logger.info("receiver started (idles until the first RTP datagram)", self_ssrc=self_ssrc)

    async def admit() -> None:
        """Datagram → RTP packet → jitter buffer."""
        nonlocal media_ssrc, peer
        while True:
            datagram, source = await socket.receive()
            PACKETS_RECEIVED_TOTAL.inc()
            BYTES_RECEIVED_TOTAL.inc(len(datagram))
            try:
                packet = RtpPacket.parse(datagram)  # V1
            except MediaError as exc:
                logger.debug("dropped rtp datagram", error=str(exc))
                continue
            peer = source
            media_ssrc = packet.header.ssrc
            buffer.insert(packet, time.monotonic())  # V2
            BUFFER_DEPTH.set(len(buffer))

    async def play_out() -> None:
        """Release whatever the buffer says is ready, on a steady tick."""
        while True:
            await asyncio.sleep(PLAYOUT_TICK)
            if not buffer:
                # Stay idle — and raise-free — until real traffic arrives.
                continue
            while (frame := buffer.pop_frame(time.monotonic())) is not None:  # V2
                _access_unit = depacketize(frame)  # V1
                # The playout sink: a decoder, a file, an eye. Nothing consumes
                # it here, which is deliberate — the SPEC grades the *timeline*
                # (was the frame released on time?), not the pixels.

    async def send_feedback() -> None:
        """NACK the gaps worth recovering, on the RTCP interval."""
        while True:
            await asyncio.sleep(FEEDBACK_INTERVAL)
            if peer is None or not buffer:
                continue
            missing = buffer.missing()  # V2
            if not missing:
                continue
            PACKETS_LOST_TOTAL.inc(len(missing))
            nack = Nack.from_missing(self_ssrc, media_ssrc, missing)
            socket.send(serialize(nack), peer)  # V3
            NACKS_TOTAL.labels(dir="sent").inc()
            # A periodic RTCP Receiver Report belongs here too — it is what
            # feeds V4 on the far side, so a sender whose bitrate never moves
            # is usually a receiver that never sent one.

    async with asyncio.TaskGroup() as group:
        group.create_task(admit(), name="receiver-admit")
        group.create_task(play_out(), name="receiver-playout")
        group.create_task(send_feedback(), name="receiver-feedback")
