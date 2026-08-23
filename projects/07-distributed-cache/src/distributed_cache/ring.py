"""V2 — The consistent-hash ring with virtual nodes.

This is what decides *which node owns a key* without a coordinator, and — the
whole reason it exists — keeps almost every key in place when the node set
changes. `hash(key) % N` fails that second property catastrophically: bump `N`
and nearly every key remaps, cold-missing the entire cache on a deploy.

The ring fixes it: hash both nodes and keys onto the same circular space, and a
key belongs to the first node you meet walking clockwise. Adding a node only
steals the arc of keys between it and its predecessor (~1/N of them); the rest
don't move. **Virtual nodes** — hashing each physical node to many ring
positions — keep the arcs evenly sized so one node doesn't randomly own half the
keyspace.

Pure data structure: no async, no locks (the caller — membership, V3 — wraps it
in one). That makes it directly unit-testable, which is exactly what the V2
proofs need. Scaffold state: `__init__` works; the ring operations raise.
"""

from __future__ import annotations

import hashlib

from .node import NodeId

__all__ = ["Ring", "ring_position"]


def ring_position(data: bytes) -> int:
    """Hash arbitrary bytes to a position on the 64-bit ring.

    SHA-256 gives a well-distributed digest; we take its leading 8 bytes as the
    position. A crypto hash is overkill for load balancing, but it avoids a real
    trap: Python's built-in `hash()` for `str` is **randomly salted per process**
    (PYTHONHASHSEED), so it would place the same node at a different ring
    position on every node and every restart. The ring must hash a node to the
    *same* position everywhere, forever.
    """
    digest = hashlib.sha256(data).digest()
    return int.from_bytes(digest[:8], "big")


class Ring:
    """Maps ring position -> physical node, spread via `vnodes_per_node`
    virtual positions per node.
    """

    def __init__(self, vnodes_per_node: int) -> None:
        if vnodes_per_node <= 0:
            raise ValueError("need at least one vnode per node")
        # How many ring positions each physical node occupies. Higher = smoother
        # load, more memory. A documented choice in the SPEC (typical: 100-200).
        self._vnodes_per_node = vnodes_per_node
        # TODO(V2): the ring itself. You need a structure that, given a key's
        # hash, finds the next position >= it in better than O(n).
        #
        # The stdlib answer is `bisect`: keep positions in a *sorted* list and
        # a parallel dict position -> NodeId (or a list of (position, node)
        # pairs kept sorted). `bisect.bisect_left(positions, h)` is the
        # clockwise walk; an index past the end wraps to 0.
        #
        # The tradeoff to notice: bisect gives O(log n) lookup but O(n) insert
        # (list splice). With ~128 vnodes x a handful of nodes that is the right
        # call, because lookups vastly outnumber membership changes. Say so in
        # the design doc rather than reaching for a third-party sorted container.

    @property
    def vnodes_per_node(self) -> int:
        return self._vnodes_per_node

    def add_node(self, node: NodeId) -> None:
        """Place a physical node onto the ring at `vnodes_per_node` positions."""
        # TODO(V2): for i in range(vnodes_per_node), insert
        # ring[ring_position(f"{node}#{i}".encode())] = node. The `i` is what
        # turns one physical node into many ring positions. Must be idempotent:
        # adding a node already present shouldn't duplicate or corrupt it.
        raise NotImplementedError("V2: insert this node's virtual nodes onto the ring")

    def remove_node(self, node: NodeId) -> None:
        """Remove a physical node and all of its virtual positions."""
        # TODO(V2): drop every ring position owned by `node`. Only the keys in
        # those arcs move (to the next node clockwise); everything else stays.
        raise NotImplementedError("V2: remove all of this node's virtual nodes from the ring")

    def owner(self, key: str) -> NodeId | None:
        """The physical node that owns `key` — the first one clockwise from the
        key's hash. `None` only when the ring is empty.
        """
        replicas = self.replicas(key, 1)
        return replicas[0] if replicas else None

    def replicas(self, key: str, n: int) -> list[NodeId]:
        """The first `n` **distinct physical** nodes clockwise from `key`'s hash
        — the key's replica set (V4 stores the value on all of them).
        """
        # TODO(V2): hash the key, walk the ring clockwise from that position
        # (wrapping past the end back to the start), and collect node ids —
        # SKIPPING vnodes that map to a physical node already chosen, because
        # several vnodes of the same node will sit next to each other. Stop at
        # `n` distinct nodes, or when you have seen every node (n > cluster size).
        raise NotImplementedError("V2: walk the ring clockwise collecting n distinct nodes")

    def node_count(self) -> int:
        """How many physical nodes are currently on the ring.

        For the balance test and for clamping the replica count.
        """
        raise NotImplementedError("V2: number of distinct physical nodes on the ring")
