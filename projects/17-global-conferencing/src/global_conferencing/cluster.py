"""Node-to-node RPC transport for the placement control plane. **Wired, not a vertical.**

Consensus is defined in terms of the messages nodes send each other; how the
bytes travel is a detail the algorithm does not care about. So the transport is
done for you, the same shape as project 09's: one `httpx.AsyncClient` that POSTs
each RPC as JSON to a peer's `/cluster/*` route and decodes the reply into a
typed model. The learning is what V1 *decides to send and how it reacts*.

Three things to carry into V1:

**Failure is the normal case.** A peer may be down, slow, or on the far side of
the backbone sag the boss fight induces. Every way a call can fail — refused,
timed out, a 5xx, a 501 from a peer whose V1 is not built yet, a reply that does
not parse — surfaces as one `PeerUnreachableError`. Election and replication
code treats that as "no answer from that region this round" and carries on. A
consensus loop that lets one refused connection escape is a mesh that stops the
first time a region blips.

**One client, pooled.** Created once in the lifespan and closed there. A client
per RPC would pay a TCP handshake per heartbeat, per peer — across an ocean,
most of the heartbeat budget.

**Canvass concurrently.** `asyncio.gather(*(client.request_vote(r, req) for r
in regions), return_exceptions=True)` asks every peer at once. Asking one at a
time makes an election take (N-1) × timeout in the worst case — longer than the
election timeout that started it, so the mesh starts the next election before
the first finishes. `return_exceptions=True` keeps one refusal from cancelling
the rest.

TODO(security): the cascade-auth checklist item. Right now anything that can
reach the HTTP port can forge a `/cluster/replicate` and commit a placement.
Whether that becomes a shared secret in a header (checked in constant time —
`hmac.compare_digest`, not `==`) or mTLS is your call and a line in
`docs/17-design.md`; this client is the one place the sending half goes.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx
from pydantic import BaseModel, ValidationError

from .config import PeerNode
from .errors import PeerUnreachableError
from .rpc import ReplicateReply, ReplicateRequest, VoteReply, VoteRequest

__all__ = ["ClusterClient"]


class ClusterClient:
    """Sends the placement RPCs to the other regional SFUs."""

    def __init__(
        self,
        peers: Iterable[PeerNode],
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._peers = {peer.region: peer for peer in peers}
        # `transport` is the test seam: an `httpx.ASGITransport` pointed at a
        # second app lets two SFUs talk in one process without a socket.
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def regions(self) -> tuple[str, ...]:
        """The peer regions this node can send to (excludes itself)."""
        return tuple(self._peers)

    async def request_vote(self, region: str, request: VoteRequest) -> VoteReply:
        """Ask `region`'s SFU for its vote (V1)."""
        return await self._post(region, "/cluster/vote", request, VoteReply)

    async def replicate(self, region: str, request: ReplicateRequest) -> ReplicateReply:
        """Append entries to (or heartbeat) `region`'s SFU (V1)."""
        return await self._post(region, "/cluster/replicate", request, ReplicateReply)

    async def _post[R: BaseModel](
        self, region: str, path: str, body: BaseModel, reply: type[R]
    ) -> R:
        peer = self._peers.get(region)
        if peer is None:
            raise PeerUnreachableError(region, "not in PEERS")
        try:
            response = await self._http.post(
                f"{peer.control_url}{path}",
                content=body.model_dump_json(),
                headers={"content-type": "application/json"},
            )
            response.raise_for_status()
            return reply.model_validate_json(response.content)
        except httpx.HTTPError as exc:
            # `from exc` keeps the real cause on the traceback for when a
            # "partition" turns out to be a typo in PEERS.
            raise PeerUnreachableError(region, str(exc)) from exc
        except ValidationError as exc:
            raise PeerUnreachableError(region, f"unparseable reply: {exc}") from exc

    async def aclose(self) -> None:
        """Close the pooled connections. Called from the lifespan on shutdown."""
        await self._http.aclose()
