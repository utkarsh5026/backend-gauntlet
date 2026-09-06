"""Typed settings for the SFU.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` binds both planes on localhost. Types here are the
parser: declaring `media_port: int` gets the env lookup, the coercion, the
default, and a startup error naming the offending variable — and declaring
`public_ip: IPv4Address | IPv6Address` gets the *validation*, so an unparseable
`PUBLIC_IP` fails at boot instead of being silently swallowed into a fallback
that makes every browser's ICE check land somewhere else.

The knobs are grouped by what they bound, because in a media server almost all
of them are a bound on something an untrusted peer controls: how many rooms it
can make you allocate, how many datagrams it can make you queue, and how much
bandwidth the estimator is ever allowed to believe in.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- The two planes ---
    media_port: int = Field(default=7000, ge=0, lt=65536)
    """UDP port carrying STUN + RTP + RTCP, muxed (RFC 7983).

    `0` means "let the kernel pick one", which is what the tests use so several
    SFUs can run in one process without colliding. Read the port that was
    actually bound off `MediaSocket.local_addr` rather than off this field —
    they are the same number only when you configured a real one."""

    http_port: int = Field(default=8080, gt=0, lt=65536)
    """Signaling (`/rooms/...`) and admin (`/healthz`, `/status`, `/metrics`)."""

    public_ip: IPv4Address | IPv6Address = IPv4Address("127.0.0.1")
    """The ICE host candidate the SFU advertises to clients.

    On a server this must be the address a browser can actually reach, not the
    one the socket is bound to — those differ behind every load balancer and
    inside every container, and getting it wrong is indistinguishable from a
    broken ICE implementation right up until you check what candidate you
    handed out."""

    log_level: str = "info"

    # --- Caps: an open UDP port and a public signaling API take input from anyone ---
    max_rooms: int = Field(default=64, gt=0)
    max_peers_per_room: int = Field(default=64, gt=0)

    media_inbox: int = Field(default=1024, gt=0)
    """Inbound datagram queue depth between the loop's receive callback and the
    pump. Bounded on purpose — see `.env.example` and `pump.py`."""

    # --- Per-subscriber bandwidth estimate bounds (V4), configured in kbps ---
    min_kbps: int = Field(default=150, gt=0)
    start_kbps: int = Field(default=1000, gt=0)
    max_kbps: int = Field(default=4000, gt=0)

    @property
    def media_addr(self) -> tuple[str, int]:
        """The address clients send their ICE checks and media to."""
        return (str(self.public_ip), self.media_port)

    @property
    def min_bitrate(self) -> int:
        """Estimator floor, bits/sec.

        The kbps->bps conversion lives here rather than at the call site so the
        estimator only ever sees one unit. Mixing kbps and bps in a control loop
        is a factor-of-1000 bug that looks exactly like a broken control law."""
        return self.min_kbps * 1000

    @property
    def start_bitrate(self) -> int:
        return self.start_kbps * 1000

    @property
    def max_bitrate(self) -> int:
        return self.max_kbps * 1000
