"""Typed settings for one regional SFU.

Every field maps to a variable in `.env.example`, and the type annotation is the
parser: `cascade_port: int` gets the env lookup, the coercion, the default and a
startup error naming the offending variable. Three things here are more than
plumbing.

**`PEERS` is the mesh.** There is no discovery service — this comma-separated
`region=<http-control-base>|<cascade-host:port>` list *is* the membership, and it
decides the quorum. So it is parsed **strictly**. The Rust scaffold skipped a
malformed entry and carried on; that is the one thing a consensus config must
never do, because a silently-dropped peer turns a 5-node mesh (quorum 3) into a
4-node one (quorum 3 of 4 is still a majority, but 2+2 partitions now both look
like minorities, and the next typo makes a minority look like a majority). A
typo fails the process at boot with the bad entry in the message.

It is a plain `str` field with a validator rather than a `list[PeerNode]` field
because pydantic-settings JSON-decodes complex-typed variables straight out of
the environment, and `us-east=http://…|10.0.0.2:7100` is not JSON.

**Timings are configured in milliseconds and used in seconds.** The environment
speaks ms because that is the readable unit for a heartbeat; asyncio speaks
seconds everywhere (`asyncio.sleep`, `asyncio.timeout`). Converting once, here,
means no call site ever holds a number in the wrong unit — the class of bug that
makes a mesh elect a leader every millisecond or every seventeen minutes.

**The cross-field rules are checked at startup.** A heartbeat at or above the
election timeout means followers time out between heartbeats from a perfectly
healthy leader, and the mesh elects forever. A region listed in its own `PEERS`
counts itself twice toward quorum. Both are configuration, not bugs in V1, and
both should fail before the first RPC rather than look like a consensus bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Self

from common_config import BaseConfig
from pydantic import Field, field_validator, model_validator

from .ids import Address

__all__ = ["PeerNode", "Settings", "parse_peers"]


@dataclass(frozen=True, slots=True)
class PeerNode:
    """Another regional SFU in the mesh."""

    region: str
    """Its region label — also its identity in consensus and the key of its relay leg."""

    control_url: str
    """HTTP base (`http://10.0.0.2:8080`) where this node POSTs the `/cluster/*` RPCs."""

    cascade_addr: Address
    """Its backbone UDP endpoint — where relay copies for that region are sent (V2)."""


def _parse_host_port(raw: str, entry: str) -> Address:
    host, sep, port = raw.rpartition(":")
    host = host.strip("[]")  # accept a bracketed IPv6 literal
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad cascade address in PEERS entry {entry!r} (want host:port)")
    return (host, int(port))


def parse_peers(raw: str) -> tuple[PeerNode, ...]:
    """Parse `PEERS` into the mesh's peer nodes. Empty means a lone SFU.

    Raises `ValueError` on anything malformed, and on a region listed twice —
    two entries for one region would give it two votes.
    """
    peers: list[PeerNode] = []
    seen: set[str] = set()
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        region, eq, rest = entry.partition("=")
        control, bar, cascade = rest.partition("|")
        region, control, cascade = region.strip(), control.strip(), cascade.strip()
        if not eq or not bar or not region or not control or not cascade:
            raise ValueError(
                f"bad PEERS entry {entry!r} (want region=<http-control-base>|<host:port>)"
            )
        if not control.startswith(("http://", "https://")):
            raise ValueError(f"control base in PEERS entry {entry!r} must be an http(s) URL")
        if region in seen:
            raise ValueError(f"region {region!r} appears twice in PEERS")
        seen.add(region)
        peers.append(
            PeerNode(
                region=region,
                control_url=control.rstrip("/"),
                cascade_addr=_parse_host_port(cascade, entry),
            )
        )
    return tuple(peers)


class Settings(BaseConfig):
    # --- identity ---
    region: str = Field(default="eu-west", min_length=1, max_length=64)
    node_id: str = Field(default="n1", min_length=1, max_length=64)

    # --- HTTP: signaling + cluster control + admin ---
    http_port: int = Field(default=8080, gt=0, lt=65536)
    log_level: str = "info"

    # --- the two UDP planes ---
    media_port: int = Field(default=7000, ge=0, lt=65536)
    """Participant-facing muxed socket (STUN/RTP/RTCP). `0` lets the kernel pick —
    the tests do that so several SFUs fit in one process. Ask the bound endpoint
    for the real port rather than reading this field."""

    cascade_port: int = Field(default=7100, ge=0, lt=65536)
    """The backbone socket relay copies arrive on (V2). Same `0` rule."""

    public_ip: IPv4Address | IPv6Address = IPv4Address("127.0.0.1")
    """The ICE host candidate advertised to clients. Validated, so an unparseable
    `PUBLIC_IP` fails at boot instead of quietly advertising somewhere else."""

    media_inbox: int = Field(default=1024, gt=0)
    cascade_inbox: int = Field(default=4096, gt=0)

    # --- the mesh ---
    peers: str = ""
    """Raw `PEERS`; read `peer_nodes` for the parsed, validated form."""

    # --- bounds ---
    max_rooms: int = Field(default=256, gt=0)
    max_peers_per_room: int = Field(default=512, gt=0)
    max_relay_links: int = Field(default=16, gt=0)

    # --- placement consensus (V1) ---
    election_timeout_ms: int = Field(default=1000, gt=0)
    heartbeat_ms: int = Field(default=300, gt=0)
    cluster_rpc_timeout_ms: int = Field(default=250, gt=0)

    # --- routing (V3) ---
    hysteresis_ticks: int = Field(default=3, ge=0)

    # --- recording (V4) ---
    recording_dir: Path = Path("./recordings")
    segment_secs: float = Field(default=6.0, gt=0)

    # --- background loops ---
    run_background: bool = False

    @field_validator("peers")
    @classmethod
    def _peers_parse(cls, raw: str) -> str:
        parse_peers(raw)  # fail at startup, naming the bad entry
        return raw

    @model_validator(mode="after")
    def _mesh_is_coherent(self) -> Self:
        if self.heartbeat_ms >= self.election_timeout_ms:
            raise ValueError(
                f"HEARTBEAT_MS ({self.heartbeat_ms}) must be below ELECTION_TIMEOUT_MS "
                f"({self.election_timeout_ms}) or followers time out on a healthy leader"
            )
        if any(peer.region == self.region for peer in self.peer_nodes):
            raise ValueError(f"REGION {self.region!r} is listed in its own PEERS")
        return self

    @property
    def peer_nodes(self) -> tuple[PeerNode, ...]:
        return parse_peers(self.peers)

    @property
    def election_timeout(self) -> float:
        """Election-timeout base, seconds."""
        return self.election_timeout_ms / 1000

    @property
    def heartbeat(self) -> float:
        """Leader heartbeat interval, seconds."""
        return self.heartbeat_ms / 1000

    @property
    def cluster_rpc_timeout(self) -> float:
        """Per-RPC timeout for `/cluster/*` calls, seconds."""
        return self.cluster_rpc_timeout_ms / 1000

    def advertised_media_addr(self, port: int) -> str:
        """`public_ip:port` as a client dials it — bracketed when the IP is IPv6."""
        host = f"[{self.public_ip}]" if isinstance(self.public_ip, IPv6Address) else self.public_ip
        return f"{host}:{port}"
