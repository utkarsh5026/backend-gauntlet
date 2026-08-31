"""Node-to-node RPC transport. **Plumbing — fully wired, not a vertical.**

Raft is defined in terms of two RPCs a node sends to its peers; how those bytes
travel is an implementation detail the algorithm doesn't care about. So this is
done for you: a thin `httpx.AsyncClient` that POSTs each RPC as JSON to the peer's
`/raft/*` endpoint and decodes the reply. The learning is the consensus logic that
*decides what to send and how to react* — not the HTTP.

Three things to notice for when you build V1/V2:

**Failure is the normal case.** These calls raise (a peer may be down, slow, or
partitioned). That is not exceptional — tolerating it is the entire reason Raft
exists. Your election and replication code must treat a failed `request_vote` /
`append_entries` as "no answer from that peer this round" and carry on. A single
`except TransportError: continue` in the right place is the difference between a
cluster that survives a dead node and one that stops.

**One client, not one per call.** The client is created once and reused, so TCP
connections stay pooled. Building an `AsyncClient` per RPC would pay a fresh
handshake on every heartbeat — at a 50 ms cadence across N peers, that is most of
what the node would be doing.

**Canvass peers concurrently.** Nothing here does that for you, but nothing stops
you either: `asyncio.gather(*(request_vote(p, args) for p in peers),
return_exceptions=True)` sends all the vote requests at once and collects
whatever came back. Awaiting them one at a time makes an election take
(N-1) x timeout in the worst case, which is longer than the election timeout that
triggered it — so the cluster would time out and start another election before
the first one finished. `return_exceptions=True` is what keeps one refused
connection from cancelling the rest of the round.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from .errors import TransportError, UnknownPeer
from .rpc import (
    AppendEntriesArgs,
    AppendEntriesReply,
    InstallSnapshotArgs,
    InstallSnapshotReply,
    NodeId,
    RequestVoteArgs,
    RequestVoteReply,
)

__all__ = ["PeerClient"]


class PeerClient:
    """Sends RPCs to the other nodes."""

    def __init__(self, peers: dict[NodeId, str], timeout: float = 0.5) -> None:
        self._peers = peers
        # A short per-RPC timeout matters: a hung peer must not stall an election
        # round. Tune it against the heartbeat interval, not against how long a
        # healthy call takes.
        self._http = httpx.AsyncClient(timeout=timeout)

    async def request_vote(self, peer: NodeId, args: RequestVoteArgs) -> RequestVoteReply:
        """Ask `peer` for its vote (V1)."""
        return await self._post(peer, "/raft/request-vote", args, RequestVoteReply)

    async def append_entries(self, peer: NodeId, args: AppendEntriesArgs) -> AppendEntriesReply:
        """Replicate entries to (or heartbeat) `peer` (V2)."""
        return await self._post(peer, "/raft/append-entries", args, AppendEntriesReply)

    async def install_snapshot(
        self, peer: NodeId, args: InstallSnapshotArgs
    ) -> InstallSnapshotReply:
        """Ship a snapshot to a `peer` that has fallen behind the compacted log (V4)."""
        return await self._post(peer, "/raft/install-snapshot", args, InstallSnapshotReply)

    async def _post[R: BaseModel](
        self, peer: NodeId, path: str, body: BaseModel, reply: type[R]
    ) -> R:
        """POST `body` as JSON to `peer`'s `path` and decode the JSON reply.

        Generic over the reply model so each call site gets a precisely-typed
        result rather than a `BaseModel` it has to narrow by hand.
        """
        addr = self._peers.get(peer)
        if addr is None:
            raise UnknownPeer()
        try:
            response = await self._http.post(
                f"http://{addr}{path}",
                content=body.model_dump_json(),
                headers={"content-type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Every transport failure funnels into one type, so consensus code has
            # exactly one thing to catch. `from exc` keeps the real cause on the
            # traceback for when a "partition" turns out to be a typo in PEERS.
            raise TransportError(f"peer {peer}: {exc}") from exc
        return reply.model_validate_json(response.content)

    async def aclose(self) -> None:
        """Close the pooled connections. Called from the lifespan on shutdown."""
        await self._http.aclose()
