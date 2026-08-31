"""Typed settings for one Raft node.

Every field maps to a variable in `.env.example`, and the type annotation is the
parser: declaring `heartbeat_ms: int` gets the env lookup, the string->int
coercion, the default, and a startup error naming the offending variable.

Two things here are more than plumbing:

**`PEERS` is the cluster.** There is no discovery service and no config server —
this comma-separated `id=host:port` list *is* the membership, and every node
reads the same one. It is kept as a plain string field with a validator rather
than a `dict[NodeId, str]` field, because pydantic-settings tries to JSON-decode
a complex-typed variable straight out of the environment, and `1=127.0.0.1:9001`
is not JSON. The validator runs the real parse at startup, so a typo in `PEERS`
fails the process immediately with a message naming the bad entry, instead of
surfacing later as a node that mysteriously never gets a vote.

**The timings are seconds, as floats.** The environment speaks milliseconds
because that is the readable unit for a heartbeat; asyncio speaks seconds
everywhere (`asyncio.sleep`, `asyncio.timeout`). Converting once here means no
call site has to remember which unit it is holding — the class of bug that makes
a cluster elect a leader every 50 ms or every 5 minutes.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field, field_validator

from .rpc import NodeId

__all__ = ["Settings", "parse_peers", "port_of"]


def parse_peers(raw: str) -> dict[NodeId, str]:
    """Parse `PEERS` — a comma-separated `id=host:port` list — into an id->addr map.

    Raises `ValueError` on anything malformed. Being strict is deliberate: a
    silently-dropped peer changes `quorum()`, and a cluster that thinks it has two
    members when it has three will happily elect two leaders' worth of trouble.
    """
    cluster: dict[NodeId, str] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        node_id, sep, addr = entry.partition("=")
        if not sep or not addr.strip():
            raise ValueError(f"bad PEERS entry {entry!r} (want `id=host:port`)")
        try:
            parsed_id = int(node_id.strip())
        except ValueError as exc:
            raise ValueError(f"bad node id in {entry!r}") from exc
        cluster[parsed_id] = addr.strip()
    if not cluster:
        raise ValueError("PEERS is empty")
    return cluster


def port_of(addr: str) -> int:
    """Extract the port from a `host:port` address, for binding."""
    _, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"no port in address {addr!r}")
    return int(port)


class Settings(BaseConfig):
    # --- identity + cluster ---

    node_id: NodeId = 1
    """This node's id. Must appear in `peers` — checked at startup."""

    peers: str = "1=127.0.0.1:9001"
    """The whole cluster as `id=host:port,...`, including this node.

    A single-node default so `make run` boots with no env at all: a one-node
    cluster has a quorum of one, which makes the "boring path" in the SPEC's
    suggested order of attack runnable before any peer exists."""

    log_level: str = "info"

    # --- storage ---

    data_dir: Path = Path("./data")
    """Each node persists its term/vote/log under `data_dir/node-<node_id>/`.
    Raft's safety proof assumes this survives a crash — the filesystem is the
    durable state, and there is no other dependency."""

    # --- timing (V1) ---

    heartbeat_ms: int = Field(default=50, gt=0)
    """Leader heartbeat cadence. Must stay well below `election_min_ms`, or
    healthy followers time out and start needless elections."""

    election_min_ms: int = Field(default=150, gt=0)
    election_max_ms: int = Field(default=300, gt=0)
    """Election timeout is drawn uniformly from `[min, max]` *per attempt*. The
    spread is what desynchronizes followers and breaks split votes — a fixed
    timeout would let the same tie repeat forever."""

    peer_timeout_ms: int = Field(default=500, gt=0)
    """Per-RPC deadline for a call to a peer. A hung peer must not stall an
    election round, so keep this under the election-timeout floor."""

    # --- compaction (V4) ---

    snapshot_threshold: int = Field(default=1000, gt=0)
    """Snapshot the state machine and compact once this many entries are
    physically retained."""

    @field_validator("peers")
    @classmethod
    def _peers_parse(cls, raw: str) -> str:
        """Fail at startup, not at the first election, if `PEERS` is malformed."""
        parse_peers(raw)
        return raw

    @property
    def cluster(self) -> dict[NodeId, str]:
        """The membership map: node id -> client-facing address."""
        return parse_peers(self.peers)

    @property
    def self_addr(self) -> str:
        """This node's own address, so it can name itself as leader in a redirect."""
        cluster = self.cluster
        if self.node_id not in cluster:
            raise ValueError(f"NODE_ID {self.node_id} not found in PEERS ({self.peers!r})")
        return cluster[self.node_id]

    @property
    def peer_addrs(self) -> dict[NodeId, str]:
        """Everyone but us — the set to canvass for votes and replicate to."""
        return {i: addr for i, addr in self.cluster.items() if i != self.node_id}

    @property
    def bind_port(self) -> int:
        """The port to serve on, taken from this node's own entry in `PEERS`.

        Deriving it rather than configuring it separately means one list defines
        the whole topology — a node cannot end up listening somewhere its peers
        are not calling."""
        return port_of(self.self_addr)

    @property
    def state_path(self) -> Path:
        """Where this node's persistent Raft state lives.

        Namespaced by node id so a 3-node cluster can share one `DATA_DIR` on a
        laptop without three processes fighting over one file."""
        return self.data_dir / f"node-{self.node_id}" / "raft-state.json"

    # --- derived timings, in the seconds asyncio wants ---

    @property
    def heartbeat_interval(self) -> float:
        return self.heartbeat_ms / 1000.0

    @property
    def election_timeout_min(self) -> float:
        return self.election_min_ms / 1000.0

    @property
    def election_timeout_max(self) -> float:
        return self.election_max_ms / 1000.0

    @property
    def peer_timeout(self) -> float:
        return self.peer_timeout_ms / 1000.0
