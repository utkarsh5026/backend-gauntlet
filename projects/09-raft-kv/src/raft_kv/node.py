"""The Raft node: the top-level owner that holds all consensus state and drives
the timers.

Plumbing/wiring — the *decisions* live in the vertical modules that take a
`RaftNode` as their first argument: elections (V1, `election.py`), replication
(V2, `replication.py`), the state machine (V3, `store.py`), and snapshots (V4,
`snapshot.py`).

## Why the verticals are functions, not methods

Rust split these across files with four `impl RaftNode` blocks. Python has no
such thing, and the usual workarounds — mixin classes, monkey-patching methods on
at import time — buy one dot of syntax (`node.start_election()`) and cost you a
type checker that can no longer see which attributes exist where. So each
vertical is a module of **plain functions taking the node explicitly**:
`await election.start_election(node)`. The call site then names the vertical it
is entering, which in a codebase organised *by* vertical is a feature. The import
direction is one-way — the vertical modules import `RaftNode` only under
`TYPE_CHECKING`, so `node.py` can import them at runtime with no cycle.

## Concurrency model — read this before you write a line of V1

Rust needed `Mutex<Inner>` because any of tokio's worker threads could touch this
state, and the rule was "never hold the lock across an `.await`". Python's rules
are different in a way that is easy to get *accidentally* right and then
catastrophically wrong:

  * **A synchronous block is already atomic.** One event loop, one thread. A
    function with no `await` in it cannot be interleaved with anything. Reading
    `role`, checking a term, and writing both back — with no `await` between — is
    a critical section for free. Most of the term/vote/role logic in V1 and V2 can
    and should be written this way.
  * **Every `await` is a yield point, and that is where the bug lives.** The
    moment you write `await peer_client.append_entries(...)`, the loop runs other
    handlers. An inbound `RequestVote` can bump the term and step this node down
    *while your election is still counting votes*. This is the exact hazard the
    Rust comment was about, arriving through a completely different door — and
    Python will not warn you, because there is no lock to hold wrongly.
  * **Hence the rule: re-check after you await.** Snapshot what you need
    (`term`, `last_log_index`) into locals, do the I/O, then before acting on the
    result confirm you are *still* the same role in the *same* term. "I won the
    election for term 4" is only true if you are still a candidate in term 4 when
    the votes come back.
  * **`self.lock` is for the multi-step critical sections that must span an
    await** — an append followed by a `persist()`, say, where a second writer
    interleaving would tear the state. Use it there, deliberately, and document
    why. Do not sprinkle it over the sync paths: an `async with` you did not need
    adds a suspension point that lets in the interleaving you were guarding
    against. That deliberate, documented model is a graded checklist item.

## The driver: two timers, no `select!`

Rust used `tokio::select!` over an election timer and a heartbeat ticker. The
asyncio shape is different and, once seen, simpler. An election timeout that
resets on every heartbeat is not a sleep — it is *"wait for a heartbeat, but give
up after t seconds"*:

```python
try:
    async with asyncio.timeout(self.random_election_timeout()):
        await self.heard_from_leader.wait()
    self.heard_from_leader.clear()          # heard one; loop and wait again
except TimeoutError:
    ...                                      # the timer fired: start an election
```

The `TimeoutError` branch *is* the election timer firing. Resetting the timer is
just setting the event (`reset_election_timer`), which any RPC handler can do
from anywhere with no shared timer object to synchronise. The leader's heartbeat
is a separate concern and wants a separate task, started when this node becomes
leader and cancelled when it steps down — running both in one loop means a
heartbeat interval that drifts with election timing.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from .log import RaftLog
from .peer import PeerClient
from .rpc import LogIndex, NodeId, Term
from .store import Store

__all__ = ["RaftConfig", "RaftNode", "Role"]

logger = structlog.get_logger(__name__)


class Role(StrEnum):
    """What a node is right now.

    Every node is exactly one of these at a time; a term has at most one leader.
    `StrEnum` so `role` serializes straight to `"leader"` in `/status` with no
    encoder — the Python equivalent of the Rust `#[serde(rename_all)]`.
    """

    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass(slots=True, frozen=True)
class RaftConfig:
    """Static cluster + timing config, built from `Settings` in `main`.

    Durations are **float seconds**, because that is what `asyncio.sleep` and
    `asyncio.timeout` take. The environment speaks milliseconds; the conversion
    happens once, in `config.py`.
    """

    heartbeat_interval: float
    """How often a leader sends heartbeats. Must be comfortably less than
    `election_timeout_min`, or followers time out under a healthy leader."""

    election_timeout_min: float
    election_timeout_max: float
    """Election timeout is drawn uniformly from `[min, max]` *per attempt*. The
    randomness is what desynchronizes followers and breaks split votes (V1)."""

    snapshot_threshold: int
    """Take a snapshot once this many entries are retained (V4)."""


@dataclass(slots=True)
class LeaderState:
    """The bookkeeping that exists only while this node is leader, reinitialized
    on winning an election.

    Grouped in its own object so "am I leader?" and "what do I know about my
    followers?" cannot drift apart: a deposed leader drops the whole thing rather
    than leaving stale `match_index` entries lying around to be counted into a
    quorum later.
    """

    next_index: dict[NodeId, LogIndex] = field(default_factory=dict[NodeId, LogIndex])
    """Per-follower: the next index the leader will try to send."""

    match_index: dict[NodeId, LogIndex] = field(default_factory=dict[NodeId, LogIndex])
    """Per-follower: the highest index known replicated — drives commit advance."""


class RaftNode:
    """One Raft node. Built once in the lifespan and shared by every handler."""

    def __init__(
        self,
        node_id: NodeId,
        config: RaftConfig,
        self_addr: str,
        peer_addrs: dict[NodeId, str],
        log: RaftLog,
        store: Store,
        peer_client: PeerClient,
    ) -> None:
        self.id = node_id
        self.config = config
        self.self_addr = self_addr
        """This node's own client-facing address, so it can name itself as leader."""

        self.peer_addrs = peer_addrs
        """Peer id -> client-facing address, for RPCs and redirect responses."""

        self.peers: list[NodeId] = list(peer_addrs)
        """The other nodes' ids — the set to canvass and replicate to."""

        # --- persistent (via `log.persist()`): term, vote, and entries ---
        self.log = log

        # --- volatile, all nodes ---
        self.role: Role = Role.FOLLOWER
        self.commit_index: LogIndex = 0
        """Highest log index known to be committed (safe to apply)."""

        self.leader_id: NodeId | None = None
        """Who this node currently believes the leader is (for client redirects)."""

        # --- volatile, leader only ---
        self.leader_state: LeaderState | None = None
        """`None` unless this node is the leader. See `LeaderState`."""

        self.store = store
        self.peer_client = peer_client

        # --- coordination ---
        self.lock = asyncio.Lock()
        """For multi-step critical sections that must span an `await`. Read the
        concurrency section of the module docstring before reaching for it."""

        self.heard_from_leader = asyncio.Event()
        """Set by any RPC handler that should reset the election timer: a valid
        `AppendEntries` from the current leader, or a vote this node granted. The
        driver waits on it; setting it is how "the timer resets" is expressed."""

    # ---- cluster arithmetic (wired) -----------------------------------------

    @property
    def cluster_size(self) -> int:
        """Cluster size including this node."""
        return len(self.peers) + 1

    @property
    def quorum(self) -> int:
        """The number of nodes (including self) that must agree for a majority.

        `N // 2 + 1`. This is the number that makes Raft work: any two majorities
        of the same set must share at least one member, so a committed entry is
        held by someone in every future election's winning set — which is why it
        can never be lost to a leader change.
        """
        return self.cluster_size // 2 + 1

    def random_election_timeout(self) -> float:
        """A fresh, randomized election timeout, in seconds.

        Drawn per *attempt*, not once at startup: the anti-split-vote mechanism is
        that two candidates who tied are overwhelmingly unlikely to tie again, and
        that only holds if each retry re-draws.
        """
        return random.uniform(  # noqa: S311 - timing jitter, not a security decision
            self.config.election_timeout_min, self.config.election_timeout_max
        )

    def reset_election_timer(self) -> None:
        """Tell the driver "a leader is alive" — the timer restarts from now.

        Call this from the RPC paths where Raft says the timer resets: a valid
        `AppendEntries` from the current leader, and a granted vote. Calling it
        anywhere else (on a *rejected* vote request, say) is how a partitioned
        node keeps a dead leader alive forever and no election ever starts.
        """
        self.heard_from_leader.set()

    # ---- state transitions (wired mechanics; the *when* is yours) ------------

    def become_follower(self, term: Term, leader: NodeId | None = None) -> None:
        """Step down to follower, adopting `term` if it is newer.

        A tiny but load-bearing helper: the "if a message carries a higher term,
        adopt it and revert to follower" rule appears in every RPC path, so it is
        centralized here. Wired mechanics; callers decide *when* (that's V1/V2)
        and must `await log.persist()` afterwards if the term changed — the vote
        that `set_current_term` just cleared has to be durably cleared too.

        Dropping `leader_state` here is the important line: a deposed leader that
        keeps its `match_index` could later count stale replication progress
        toward a quorum.
        """
        if term > self.log.current_term:
            self.log.set_current_term(term)
        self.role = Role.FOLLOWER
        self.leader_id = leader
        self.leader_state = None
        logger.info("stepped down to follower", term=term, leader=leader)

    # ---- read-only views (wired) --------------------------------------------

    def status(self) -> dict[str, Any]:
        """A cheap status snapshot for `GET /status` and metrics.

        Synchronous and `await`-free, so what it reports is one consistent instant
        — a status that awaited partway through could show a term from before a
        step-down next to a role from after it.
        """
        leader_state = self.leader_state
        return {
            "id": self.id,
            "role": self.role,
            "term": self.log.current_term,
            "leader_id": self.leader_id,
            "commit_index": self.commit_index,
            "last_applied": self.store.last_applied,
            "log_last_index": self.log.last_index,
            "cluster_size": self.cluster_size,
            "match_index": dict(leader_state.match_index) if leader_state else None,
        }

    def leader_hint(self) -> tuple[NodeId | None, str | None]:
        """Resolve the current leader's client address for a redirect (used by
        `errors.NotLeader`)."""
        leader = self.leader_id
        if leader is None:
            return None, None
        addr = self.self_addr if leader == self.id else self.peer_addrs.get(leader)
        return leader, addr

    def not_leader(self) -> Exception:
        """Build the `NotLeader` error with whatever redirect hint we have.

        Imported lazily to keep `errors` free of any dependency on the node.
        """
        from .errors import NotLeader

        leader_id, leader_addr = self.leader_hint()
        return NotLeader(leader_id=leader_id, leader_addr=leader_addr)

    # ---- the driver ----------------------------------------------------------

    async def run(self) -> None:
        """The driver loop — the clock that makes Raft go. Spawned once from the
        lifespan in `main`.

        This is the wiring seam for V1 + V2. The scaffold boots the node into an
        idle follower and *does not* start consensus, so the process comes up
        clean and serves `/status` and `/healthz`; the first client write or
        inbound RPC is what raises `NotImplementedError`. Replace this body with
        the real loop:

        TODO(V1/V2): the election-timer loop described in the module docstring —
        wait on `heard_from_leader` with an `asyncio.timeout`, and on
        `TimeoutError` (as follower *or* candidate) run
        `election.start_election(self)`.

        TODO(V2): a heartbeat task, started on becoming leader and cancelled on
        stepping down, ticking every `config.heartbeat_interval` into
        `replication.broadcast_append_entries(self)`.

        TODO(V2/V3/V4): whenever `commit_index` advances, apply
        `commit_index - last_applied` entries to the store in order
        (`replication.apply_committed`), then give `snapshot.maybe_snapshot` a
        chance to compact.

        Two things to get right that are easy to miss: **hold a reference to
        every task you spawn** (a bare `asyncio.create_task(...)` whose result
        nobody keeps can be garbage-collected mid-flight, taking your heartbeat
        with it), and **let `asyncio.CancelledError` propagate** on shutdown
        rather than swallowing it in a broad `except Exception`.
        """
        logger.warning(
            "raft driver started in SCAFFOLD mode — consensus not implemented "
            "(see TODO(V1/V2) in node.py::run). Node will idle as a follower.",
            node=self.id,
        )
        # Idle until cancelled. The real loop replaces this with the two timers
        # described above.
        await asyncio.Event().wait()
