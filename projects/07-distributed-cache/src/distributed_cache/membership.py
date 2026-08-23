"""V3 — Gossip membership & failure detection (SWIM).

This is the layer you'd normally get from a service registry (ZooKeeper, etcd,
Consul). Here the nodes agree on who's alive *among themselves*, with no central
authority, using SWIM:

  * **Failure detection:** each round, ping one random peer. No ack? Ask `k`
    other peers to ping it *for* you (indirect probe) before you suspect it — so
    one dropped packet doesn't evict a healthy node.
  * **Dissemination:** piggyback membership updates on those pings (gossip), so
    news of a join/death spreads in O(log n) rounds with constant per-node
    message load — not the O(n^2) of all-to-all heartbeating.
  * **Suspicion + incarnation:** a suspected node gets a grace window to
    *refute* (bump its incarnation number) before it's declared dead, killing
    the flapping you'd get from a naive timeout.

This module owns the authoritative member list **and the ring (V2)** — a
membership change is exactly when the ring must be rebuilt, so they live
together behind one lock. The coordinator (V4) reads this to route requests.

Scaffold state: the node binds its UDP socket, seeds itself into its own member
list, and the receive loop is wired — so `GET /cluster` shows this node. The
gossip *protocol* (probing, suspicion, applying updates) raises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, Field, TypeAdapter

from .node import Address, Node, NodeId
from .ring import Ring

__all__ = [
    "Ack",
    "GossipMessage",
    "Join",
    "Member",
    "MemberState",
    "MemberUpdate",
    "Membership",
    "Ping",
    "PingReq",
]

log = structlog.get_logger(__name__)

# Gossip datagrams must fit comfortably inside a single UDP packet — that ceiling
# is why the SPEC caps how many updates you piggyback per message.
MAX_DATAGRAM_BYTES = 64 * 1024

# Bounded inbox: a flood of datagrams must cost bounded memory, not an
# ever-growing backlog. Overflow is *dropped*, which is the correct answer for
# gossip — SWIM already assumes loss, and a dropped ping just costs one round.
# (SPEC, cross-cutting: "the gossip path is datagram-bounded".)
INBOX_SIZE = 1024


class MemberState(StrEnum):
    """Where a member sits in the SWIM lifecycle.

    `alive -> suspect -> dead`, with a refutation path back to `alive` via a
    higher incarnation number.
    """

    ALIVE = "alive"
    SUSPECT = "suspect"
    DEAD = "dead"


class Member(BaseModel):
    """One node in this node's view of the cluster."""

    node: Node
    state: MemberState
    # Monotonic per-node counter. A node refutes a false `suspect` by
    # re-broadcasting itself `alive` at a higher incarnation; peers keep the
    # highest incarnation they have seen, so stale gossip can't resurrect or
    # re-kill a node. This is the anti-flapping mechanism.
    incarnation: int = 0


class MemberUpdate(BaseModel):
    """A single membership fact, piggybacked on gossip messages so news spreads
    without dedicated broadcast traffic.
    """

    node: Node
    state: MemberState
    incarnation: int


# --- the SWIM wire protocol ---------------------------------------------------
# Rust modelled this as one `enum GossipMessage`. The Python equivalent of a
# tagged union is a **discriminated union**: each variant carries a literal `kind`
# field, and pydantic picks the right model from it on parse. You get the same
# exhaustive `match` at the other end, plus validation of anything off the wire.
# JSON for legibility while you build; a binary codec is a later optimisation.


class Join(BaseModel):
    """Sent to a seed on startup to enter the cluster."""

    kind: Literal["join"] = "join"
    node: Node


class Ping(BaseModel):
    """Direct failure-detection probe."""

    kind: Literal["ping"] = "ping"
    sender: NodeId
    updates: list[MemberUpdate] = []


class Ack(BaseModel):
    """Reply proving liveness."""

    kind: Literal["ack"] = "ack"
    sender: NodeId
    updates: list[MemberUpdate] = []


class PingReq(BaseModel):
    """ "Please ping `target` for me and relay the ack" — the indirect probe that
    prevents a single lost packet from evicting a healthy node.
    """

    kind: Literal["ping_req"] = "ping_req"
    sender: NodeId
    target: NodeId
    updates: list[MemberUpdate] = []


GossipMessage = Annotated[Join | Ping | Ack | PingReq, Field(discriminator="kind")]

GOSSIP_ADAPTER: TypeAdapter[GossipMessage] = TypeAdapter(GossipMessage)


@dataclass(slots=True)
class View:
    """The mutable cluster view: the member table and the hash ring derived from
    it. They change together — every membership transition rebuilds the ring so
    ownership always follows the live set.
    """

    members: dict[NodeId, Member]
    ring: Ring


class _GossipProtocol(asyncio.DatagramProtocol):
    """Bridges asyncio's callback-based UDP transport into an awaitable inbox.

    `create_datagram_endpoint` is the portable way to do UDP here: the tempting
    `loop.sock_recvfrom` exists on the stdlib event loop but **not on uvloop**,
    which is the loop uvicorn actually runs in production. Code that works under
    pytest and dies in Docker is exactly the bug this avoids.
    """

    def __init__(self, inbox: asyncio.Queue[tuple[bytes, Address]]) -> None:
        self._inbox = inbox

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        try:
            self._inbox.put_nowait((data, Address(str(addr[0]), addr[1])))
        except asyncio.QueueFull:
            log.warning("gossip inbox full; dropping datagram", peer=f"{addr[0]}:{addr[1]}")

    def error_received(self, exc: Exception) -> None:
        # UDP surfaces ICMP errors here (e.g. port unreachable). Not fatal: the
        # failure detector is supposed to notice a dead peer on its own.
        log.warning("gossip transport error", error=str(exc))


class Membership:
    """Owns the view, the UDP transport, and (once you build it) the gossip driver.

    Construct with `await Membership.bind(...)` — binding a socket is I/O, and
    `__init__` cannot be async.
    """

    def __init__(
        self,
        self_node: Node,
        seeds: list[Address],
        transport: asyncio.DatagramTransport,
        inbox: asyncio.Queue[tuple[bytes, Address]],
        view: View,
    ) -> None:
        self._self_node = self_node
        self._seeds = seeds
        self._transport = transport
        self._inbox = inbox
        self._view = view

    @classmethod
    async def bind(cls, self_node: Node, seeds: list[Address], vnodes_per_node: int) -> Membership:
        """Bind the gossip UDP endpoint and seed the view with *this* node (alive).

        Note it does **not** yet place the node on the ring — see the TODO:
        wiring membership -> ring is part of V3, and until `Ring.add_node` (V2)
        exists there is nothing to add.
        """
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[tuple[bytes, Address]] = asyncio.Queue(maxsize=INBOX_SIZE)
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _GossipProtocol(inbox),
            local_addr=(self_node.gossip.host, self_node.gossip.port),
        )
        log.info("gossip socket bound", gossip_addr=self_node.gossip_addr)

        members = {
            self_node.id: Member(node=self_node, state=MemberState.ALIVE, incarnation=0),
        }
        ring = Ring(vnodes_per_node)
        # TODO(V3): seed the ring with self — `ring.add_node(self_node.id)` —
        # once V2's `add_node` is implemented, and call it again whenever a peer
        # transitions to/from alive so ownership tracks the live set.

        return cls(self_node, seeds, transport, inbox, View(members=members, ring=ring))

    @property
    def self_id(self) -> NodeId:
        return self._self_node.id

    def replicas(self, key: str, n: int) -> list[NodeId]:
        """The `n` replica node ids for a key, read off the current ring (V2).

        The coordinator (V4) turns these into addresses and filters the dead.
        """
        return self._view.ring.replicas(key, n)

    def resolve(self, node_id: NodeId) -> Node | None:
        """Resolve a node id to its reachable address, if we know it."""
        member = self._view.members.get(node_id)
        return member.node if member else None

    def is_alive(self, node_id: NodeId) -> bool:
        """Is this node currently believed alive? (Coordinator skips dead replicas.)"""
        member = self._view.members.get(node_id)
        return member is not None and member.state is MemberState.ALIVE

    def snapshot(self) -> list[Member]:
        """A snapshot of the whole membership view.

        Backs `GET /cluster` and the membership-size metric. Real even before
        gossip works (it shows this node).
        """
        return list(self._view.members.values())

    async def run(self) -> None:
        """Drive SWIM forever: a receive loop + (TODO) a probe ticker.

        Started from the app lifespan. Awaits the inbox, so an idle node is
        quiet — it only comes alive once a peer sends the first Join/Ping.
        """
        # TODO(V3): the two concurrent halves of SWIM. Run them together with
        # `async with asyncio.TaskGroup() as tg:` — structured concurrency means
        # if either half raises, the other is cancelled and the error propagates
        # instead of vanishing into a forgotten task:
        #   1. RECEIVE loop (below): recvfrom -> parse GossipMessage ->
        #      handle_message -> apply piggybacked updates.
        #   2. PROBE ticker: every PROBE_INTERVAL pick a *random* alive member,
        #      Ping it; on no Ack, PingReq via `k` others; still nothing -> mark
        #      suspect; after the suspicion timeout -> dead. On any membership
        #      change, rebuild the ring.
        # Also: on startup, send `Join` to each seed so this node enters the
        # cluster.
        while True:
            data, sender = await self._inbox.get()
            try:
                await self.handle_datagram(data, sender)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad peer must not kill gossip
                log.warning("bad gossip datagram", error=str(exc), peer=str(sender))

    async def handle_datagram(self, data: bytes, sender: Address) -> None:
        """Decode one datagram and act on it.

        The decode is plumbing; the *acting* — state transitions, replying,
        merging updates — is the V3 learning.
        """
        message = GOSSIP_ADAPTER.validate_json(data)
        # TODO(V3): dispatch on the message. `match message:` with `case Join():`
        # etc. gives you the exhaustive branch Rust's enum did:
        #   - Join    -> add the joiner (alive), reply/gossip it onward;
        #   - Ping    -> apply updates, reply Ack (piggybacking your own);
        #   - Ack     -> apply updates, mark the prober's probe satisfied;
        #   - PingReq -> ping `target`; relay its Ack back to `sender`.
        # Merging an update = keep the entry with the higher incarnation, and let
        # this node refute a `suspect` about *itself* by bumping its incarnation.
        # Any transition that changes the live set must rebuild the ring.
        del message, sender
        raise NotImplementedError("V3: handle a decoded gossip message and merge its updates")

    def send(self, message: GossipMessage, to: Address) -> None:
        """Fire one datagram at a peer. Wired — the protocol above is yours.

        Not a coroutine: UDP `sendto` on a datagram transport buffers and returns
        immediately. There is nothing to await and no delivery to confirm — which
        is the whole reason SWIM is built on it.
        """
        self._transport.sendto(GOSSIP_ADAPTER.dump_json(message), (to.host, to.port))

    def close(self) -> None:
        self._transport.close()
