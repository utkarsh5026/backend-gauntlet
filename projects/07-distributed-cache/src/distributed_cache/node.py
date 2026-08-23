"""Shared identity types used across the ring (V2), membership (V3) and the
coordinator (V4). Not a vertical of its own — just the vocabulary the
interesting modules agree on.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel

__all__ = ["Address", "Node", "NodeId"]

# A stable, human-readable node identity (the NODE_ID env var, e.g. `cache-a`).
# The ring hashes *this string* to place a node's virtual nodes, so it must be
# stable across restarts — an id derived from a random port would reshuffle the
# whole ring on every reboot.
NodeId = str


class Address(NamedTuple):
    """A `host:port` pair, parsed once so nothing downstream splits strings."""

    host: str
    port: int

    @classmethod
    def parse(cls, raw: str) -> Address:
        host, sep, port = raw.strip().rpartition(":")
        if not sep or not host:
            raise ValueError(f"bad address {raw!r}: want host:port")
        try:
            return cls(host, int(port))
        except ValueError as exc:
            raise ValueError(f"bad port in address {raw!r}") from exc

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class Node(BaseModel):
    """Everything the cluster needs to reach a peer: who it is, where its data
    API lives (HTTP/TCP), and where its gossip endpoint lives (UDP).

    This is what gets gossiped around in V3, so it has to serialize — a pydantic
    model gives us that plus validation of anything arriving off the wire.
    Addresses are carried as `host:port` strings to keep gossip datagrams
    readable while you build the protocol.
    """

    id: NodeId
    http_addr: str
    gossip_addr: str

    @property
    def http(self) -> Address:
        return Address.parse(self.http_addr)

    @property
    def gossip(self) -> Address:
        return Address.parse(self.gossip_addr)

    def http_url(self, path: str) -> str:
        return f"http://{self.http_addr}{path}"
