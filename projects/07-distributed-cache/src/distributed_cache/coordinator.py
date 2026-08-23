"""V4 — Replication & request coordination.

Sharding alone (V2 + V3) means losing a node loses its whole shard. This layer
makes the cache *survivable* and *location-transparent*:

  * **Replication:** a key lives on the first `replication_factor` nodes
    clockwise on the ring, so one node's death doesn't lose the value.
  * **Coordination:** any node can take any request. It asks the ring who owns
    the key; if it is one of the owners it serves locally, otherwise it forwards
    to an owner and proxies the answer back. The client never learns the
    topology.

The consistency you offer is a *choice you make and document* — this is a cache,
so a common pick is W=1 + async replication (fast, may briefly stale) rather than
a database's R+W>N quorum. Name it in `docs/07-design.md`.

Scaffold state: the coordinator is wired with the local store, membership and a
shared HTTP client, and the *local* operations (used when a forwarded request
lands on an owner) call straight into the store. The routing brain raises.
"""

from __future__ import annotations

import httpx

from .membership import Membership
from .node import Node, NodeId
from .store import Store

__all__ = ["Coordinator"]


class Coordinator:
    """Routes cache operations to the nodes that own each key, replicating
    writes and proxying reads.
    """

    def __init__(
        self,
        self_id: NodeId,
        store: Store,
        membership: Membership,
        replication_factor: int,
        http: httpx.AsyncClient,
    ) -> None:
        if replication_factor < 1:
            raise ValueError("replication factor must be >= 1")
        self._self_id = self_id
        self._store = store
        self._membership = membership
        self._replication_factor = replication_factor
        # One shared client for the whole process, injected rather than created
        # here: it holds the connection pool. Building a client per request would
        # give you a fresh TCP + TLS handshake every forward — the single most
        # common way a Python service quietly loses its p99.
        self._http = http

    @property
    def replication_factor(self) -> int:
        return self._replication_factor

    def replica_nodes(self, key: str) -> list[Node]:
        """The alive replica nodes for `key`, in ring order.

        Helper the routing methods lean on once V2/V3 make `replicas`/`resolve`
        real.
        """
        nodes: list[Node] = []
        for node_id in self._membership.replicas(key, self._replication_factor):
            if not self._membership.is_alive(node_id):
                continue
            node = self._membership.resolve(node_id)
            if node is not None:
                nodes.append(node)
        return nodes

    def owns(self, node: Node) -> bool:
        return node.id == self._self_id

    async def get(self, key: str) -> bytes | None:
        """GET a key from wherever it lives.

        If this node is a replica, serve locally; otherwise forward to one and
        proxy the bytes back.
        """
        # TODO(V4): resolve replica_nodes(key). If empty -> raise Unavailable. If
        # one is *this* node -> self.local_get(key). Otherwise forward the GET to
        # a replica's http addr (GET /internal/cache/{key}) via self._http and
        # return its answer; on a replica error, try the next replica before
        # giving up (read failover). This is where you decide your read policy
        # (R=1 vs read-repair).
        raise NotImplementedError("V4: route a GET to a replica (local if we own it, else forward)")

    async def put(self, key: str, value: bytes, ttl: float | None = None) -> None:
        """PUT a key onto all of its replicas per the write policy."""
        # TODO(V4): resolve replica_nodes(key). Write to each: local ones via
        # self.local_put, remote ones via PUT /internal/cache/{key}. Your write
        # policy decides when to ack — W=1 (ack after the first, replicate the
        # rest in the background) or W=majority. `asyncio.gather` fans out; note
        # that a background replication task must be *held* somewhere or the
        # event loop may garbage-collect it mid-flight. Document which policy and
        # why (it's a cache: availability usually wins).
        raise NotImplementedError("V4: replicate a PUT to the key's replica set")

    async def delete(self, key: str) -> None:
        """DELETE a key from all of its replicas."""
        # TODO(V4): same fan-out as put — remove on every replica (local + remote
        # via DELETE /internal/cache/{key}).
        raise NotImplementedError("V4: delete a key from all of its replicas")

    # --- local operations ----------------------------------------------------
    # These run when *this* node is an owner (a directly-addressed request, or
    # one forwarded to us by a peer coordinator). They only touch the local
    # store — no routing — so they are wired now; the internal HTTP routes call
    # straight into them.

    def local_get(self, key: str) -> bytes | None:
        return self._store.get(key)

    def local_put(self, key: str, value: bytes, ttl: float | None = None) -> None:
        self._store.put(key, value, ttl)

    def local_delete(self, key: str) -> bool:
        return self._store.remove(key)
