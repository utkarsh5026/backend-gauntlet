"""Typed settings for one cache node.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts a single standalone node. Types here are the
parser: declaring `port: int` gets the env lookup, the coercion, the default, and
a startup error naming the offending variable.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field, field_validator, model_validator

from .node import Address, Node
from .store import EvictionPolicy

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- identity & networking ---
    port: int = 8070
    """Client + internal HTTP API port."""

    gossip_port: int = 7070
    """SWIM gossip UDP port."""

    advertise_host: str = "127.0.0.1"
    """Address peers use to reach this node (the compose service name in Docker)."""

    node_id: str = ""
    """Stable ring identity. Defaults to `advertise_host:port` when unset."""

    seeds: list[Address] = []
    """Seed peers to join through. Empty = this node is the first / standalone."""

    # --- local store (V1) ---
    cache_capacity: int = Field(default=100_000, gt=0)
    eviction_policy: EvictionPolicy = EvictionPolicy.LRU
    max_value_bytes: int = Field(default=1024 * 1024, gt=0)

    # --- ring & replication (V2 / V4) ---
    vnodes_per_node: int = Field(default=128, gt=0)
    replication_factor: int = Field(default=2, ge=1)

    log_level: str = "info"

    @field_validator("seeds", mode="before")
    @classmethod
    def _split_seeds(cls, raw: object) -> object:
        """Accept `SEEDS="host:port,host:port"` from the environment."""
        if not isinstance(raw, str):
            return raw
        return [Address.parse(part) for part in raw.split(",") if part.strip()]

    @model_validator(mode="after")
    def _default_node_id(self) -> Settings:
        if not self.node_id:
            self.node_id = f"{self.advertise_host}:{self.port}"
        return self

    @property
    def advertised(self) -> Node:
        """What we tell peers — the reachable host, never 0.0.0.0."""
        return Node(
            id=self.node_id,
            http_addr=f"{self.advertise_host}:{self.port}",
            gossip_addr=f"{self.advertise_host}:{self.gossip_port}",
        )

    @property
    def bind_node(self) -> Node:
        """What we actually bind: all interfaces, but advertising the real host."""
        return Node(
            id=self.node_id,
            http_addr=f"{self.advertise_host}:{self.port}",
            gossip_addr=f"0.0.0.0:{self.gossip_port}",
        )
