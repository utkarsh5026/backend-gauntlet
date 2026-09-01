"""Typed settings — every dial the client, the tracker and the seeder read.

One field per variable in `.env.example`, and the type annotation *is* the
parser: `peer_port: int = 6819` gets you the env lookup, the string->int
coercion, the default, and a startup error naming the offending variable.

The names echo the systems these dials came from, because the reading you will
do is written in their vocabulary: `upload_slots` is BitTorrent's own term for
the choke algorithm's regular unchokes, `max_peers` is what every client calls
its global connection cap, and `pipeline_depth` is the number BEP 3 is talking
about when it says to keep several requests outstanding.

## The two numbers that are not tunables

`BLOCK_SIZE` (16 KiB) and the 68-byte handshake are **protocol constants**, not
configuration, and they live in `peer.py` with the rest of the wire format. The
line between the two is worth drawing deliberately: a dial you can change and
still interoperate belongs here; a number both ends must agree on is part of the
protocol, and putting it in `.env` invites someone to "tune" their way out of
the swarm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]

DEFAULT_PORT = 8080
"""The HTTP control plane. Never peer data."""

DEFAULT_PEER_PORT = 6819
"""BitTorrent clients conventionally listen for inbound peers on 6881-6889; the
project-scoped default is 6881 with the last two digits replaced by the project
number, so nothing in the gauntlet collides."""


class Settings(BaseConfig):
    # --- listeners ---

    port: int = Field(default=DEFAULT_PORT, gt=0, lt=65536)
    """The HTTP control plane: `POST /torrents`, `GET /torrents`, `/healthz`,
    `/metrics`."""

    peer_port: int = Field(default=DEFAULT_PEER_PORT, ge=0, lt=65536)
    """The port we listen on for inbound peers, and the port we *advertise* in
    every announce so peers can dial us back.

    `0` is allowed and means "let the OS pick a free port" — which is how the
    tests bind without racing each other, and how you run two clients side by
    side to have them swarm each other. Note that a real run must advertise the
    port it actually bound, not this value: announcing `0` tells the tracker
    nothing can reach you.
    """

    log_level: str = "info"

    # --- storage ---

    download_dir: Path = Path("./data")
    """Where piece data is written and served from. Both halves of the project
    meet here: V5 writes verified pieces into it, V6 reads blocks back out."""

    disk_workers: int = Field(default=4, gt=0)
    """Size of the thread pool that serves piece reads and writes.

    Piece I/O is `os.pread`/`os.pwrite` — blocking syscalls with no async
    equivalent in CPython that is not a thread pool wearing a costume. Run them
    on the event loop and a single slow read stalls *every* peer session in the
    process, which under the boss fight is the difference between 50 leechers
    and none. This is the checklist's "bounded pool sized on purpose": the
    number is chosen against `max_peers`, not independently of it, and the
    reasoning goes in `docs/19-design.md`.
    """

    # --- resource caps ---

    max_peers: int = Field(default=50, gt=0)
    """Global cap on simultaneous peer connections, inbound and outbound.

    A cap, not a target. The reason it exists is that file descriptors and
    per-connection buffers are finite, and the failure mode when they run out is
    that the process stops accepting *everything* — including whatever you would
    have used to diagnose it.
    """

    max_message_bytes: int = Field(default=1024 * 1024, gt=0)
    """Cap on a peer message's declared length (V4).

    Enforced against the number in the 4-byte length prefix **before** anything
    is allocated for it. Checking how many bytes have actually arrived is
    checking the wrong thing: a hostile peer sends `0xFFFFFFFF` and then nothing,
    and a client that reserves first is dead before the second packet.
    """

    pipeline_depth: int = Field(default=8, gt=0)
    """Block requests kept in flight per peer (V5).

    With a depth of 1 your throughput is `BLOCK_SIZE / RTT` no matter how fast
    the peer is — 16 KiB over a 50 ms link is 320 KB/s, and no amount of
    bandwidth changes it. This is the dial that makes the pipelining criterion
    observable, and its cost is memory: depth x peers x 16 KiB of blocks in
    flight, which is why it is bounded rather than "as many as possible".
    """

    # --- tracker (V3) ---

    announce_timeout_seconds: float = Field(default=15.0, gt=0)
    """Per-announce deadline for both transports.

    Serves the criterion "one dead tracker doesn't sink the download". A tracker
    that accepts your connection and then never answers is the common case, not
    the exotic one, and without a deadline it is indistinguishable from a slow
    one forever.
    """

    # --- seeder (V6) ---

    upload_slots: int = Field(default=4, gt=0)
    """Regular upload slots for the choke algorithm.

    At most this many peers are unchoked at once, plus one optimistic unchoke.
    This is the number the boss fight checks through the `bt_peers_unchoked`
    gauge, and the reason a single seed can survive a flash crowd at all: finite
    upload bandwidth split 50 ways is 50 useless trickles.
    """

    run_seeder: bool = False
    """The seeder's accept loop is off by default so the bare scaffold serves the
    control plane without raising on its first inbound peer. Flip it on once V6
    exists."""

    # --- derived ---

    @property
    def data_dir(self) -> Path:
        """Alias for `download_dir`, spelled the way the rest of the gauntlet
        spells it. One name on the wire, one name in the code, converted once."""
        return self.download_dir

    def public_status(self) -> dict[str, Any]:
        """The configuration that is safe to publish on `GET /config`.

        Enumerated positively — an allowlist, not `model_dump()` minus a
        denylist. The two look equivalent right up until someone adds a secret
        field, at which point the denylist silently starts leaking and the
        allowlist silently keeps not leaking.

        This project's secret is subtler than a password: the SPEC's security
        item is that the tracker `key` and `peer_id` are never logged or
        published alongside anything that would deanonymize a peer. Neither is
        in this dict, and neither should join it.
        """
        return {
            "peer_port": self.peer_port,
            "download_dir": str(self.download_dir),
            "disk_workers": self.disk_workers,
            "max_peers": self.max_peers,
            "max_message_bytes": self.max_message_bytes,
            "pipeline_depth": self.pipeline_depth,
            "announce_timeout_seconds": self.announce_timeout_seconds,
            "upload_slots": self.upload_slots,
            "run_seeder": self.run_seeder,
        }
