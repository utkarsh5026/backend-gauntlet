"""Typed settings for the transport.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` binds both planes on localhost. The type
annotation *is* the parser: declaring `rtp_port: int` gets the env lookup, the
coercion, the default and a startup error naming the offending variable —
and declaring `role: Role` gets the *validation*, so `ROLE=sendr` fails at boot
instead of silently falling through to a receiver that never sends anything.

That last part is a real change from the Rust, which warned and defaulted to
receiver on an unknown role. Defaulting a typo into the opposite behaviour is
the kind of thing you debug for an hour with `tcpdump`; failing at startup with
the variable name in the message takes ten seconds.

The knobs group by what they bound, because in a media transport almost every
one of them bounds something an untrusted peer controls: how many datagrams it
can make you queue, how much memory the jitter buffer and retransmit cache can
hold on its behalf, and how much bandwidth the estimator is ever allowed to
believe in.
"""

from __future__ import annotations

from enum import StrEnum

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Role", "Settings"]


class Role(StrEnum):
    """Which direction this process runs the socket.

    One process, one role, one socket. `StrEnum` rather than a plain enum so
    `ROLE=sender` in the environment parses directly and the value renders as
    `"sender"` in `/status` and in logs without a conversion at either end.
    """

    SENDER = "sender"
    RECEIVER = "receiver"


class Settings(BaseConfig):
    # --- The two planes ---
    role: Role = Role.RECEIVER
    """`receiver` binds and consumes; `sender` produces and needs `remote_addr`."""

    rtp_port: int = Field(default=5004, ge=0, lt=65536)
    """UDP port for the media plane (RTP, and RTCP muxed alongside it).

    `0` means "let the kernel pick one", which is what the tests use so several
    transports can run in one process without colliding. Read the port that was
    actually bound off `MediaSocket.local_addr` rather than off this field —
    they are the same number only when you configured a real one."""

    http_port: int = Field(default=8080, gt=0, lt=65536)
    """Admin only: `/healthz`, `/readyz`, `/status`, `/metrics`. No media."""

    remote_addr: str | None = None
    """Sender only: `host:port` of the receiver. Parsed by `remote` below."""

    log_level: str = "info"

    # --- Packetization (V1) ---
    mtu: int = Field(default=1200, gt=0, le=65507)
    """Largest UDP payload this transport will emit, header included.

    1200 stays under a 1500-byte path MTU with room for IP/UDP and a tunnel, so
    nothing relies on IP-layer fragmentation — V1 fragments at the application
    layer instead, which is the whole point of the criterion."""

    payload_type: int = Field(default=96, ge=0, le=127)
    """7-bit RTP payload type; 96 is a dynamic mapping, typical for H.264."""

    # --- Jitter buffer (V2) ---
    playout_ms: int = Field(default=100, ge=0)
    """Target playout delay. Bigger absorbs more jitter and costs more latency —
    the tradeoff the buffer *is*. The boss fight caps the added latency at
    150 ms, so this is the knob that criterion is about."""

    jitter_capacity: int = Field(default=4096, gt=0)
    """Hard cap on buffered packets — the OOM guard against a peer that floods
    future sequence numbers or never marks a frame."""

    # --- Retransmission (V3) ---
    rtx_cache_packets: int = Field(default=1024, gt=0)
    """How many recently sent packets the sender keeps to answer NACKs.

    Also the staleness bound: a packet evicted from this ring is one you have
    decided is too old to usefully retransmit. At 1.5 Mbps in 1200-byte packets
    that is about 6.5 s of history — far more than any playout deadline, which
    means the *deadline* check is what has to do the real work, not eviction.
    Sizing it is a design-doc decision."""

    # --- Congestion control (V4), configured in kilobits/sec ---
    min_kbps: int = Field(default=300, gt=0)
    start_kbps: int = Field(default=1500, gt=0)
    max_kbps: int = Field(default=4000, gt=0)

    # --- Synthetic media source (sender, when no real RTP feed is used) ---
    fps: int = Field(default=30, gt=0, le=240)
    gop: int = Field(default=60, gt=0)
    """Keyframe interval, in frames."""

    # --- Python-specific: the loop's inbound queue ---
    rtp_inbox: int = Field(default=1024, gt=0)
    """Inbound datagram queue depth between the loop's receive callback and the
    session loop. Bounded on purpose — see `udp.py` on why an unbounded queue on
    an open UDP port is a remote OOM with no authentication in front of it."""

    @property
    def remote(self) -> tuple[str, int] | None:
        """`remote_addr` as a `(host, port)` pair, or `None` if unset.

        Parsed here rather than at the send site so the split happens once. It
        returns `None` on anything unparseable instead of raising: a sender
        without a valid destination is a startup-time complaint (`main` makes
        it), not an exception thrown from inside a hot loop.
        """
        if not self.remote_addr:
            return None
        host, _, port = self.remote_addr.rpartition(":")
        if not host or not port.isdigit():
            return None
        return (host.strip("[]"), int(port))

    @property
    def target_playout(self) -> float:
        """Playout delay in **seconds**, the unit every clock in this process uses.

        `time.monotonic()` returns float seconds, so converting once here keeps
        milliseconds out of the jitter buffer entirely. Two time units in one
        control loop is a factor-of-1000 bug that looks exactly like a broken
        policy."""
        return self.playout_ms / 1000.0

    @property
    def min_bitrate(self) -> int:
        """Estimator floor, bits/sec. The kbps→bps conversion lives here so the
        controller only ever sees one unit."""
        return self.min_kbps * 1000

    @property
    def start_bitrate(self) -> int:
        return self.start_kbps * 1000

    @property
    def max_bitrate(self) -> int:
        return self.max_kbps * 1000
