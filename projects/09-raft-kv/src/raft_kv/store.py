"""V3 — The replicated key-value state machine.

Consensus (V1 + V2) produces one thing: an agreed-upon, totally-ordered sequence
of committed `Command`s, identical on every node. This module is what *consumes*
that sequence. `apply` folds each committed command into an in-memory `dict`, **in
log order, exactly once, on every node** — which is why every node's map ends up
identical. That determinism is the whole payoff of Raft: the log is the source of
truth, the map is a cache of "the log, reduced".

The subtle part isn't `Set`/`Delete` — it's the two guarantees around them:

  * **Apply order == commit order, never skipping.** `apply` may only run for
    `last_applied + 1`. Applying out of order, or twice, silently diverges nodes.
    That sequencing is driven from the replication layer (V2), and enforced here.
  * **Linearizable reads.** A `GET` that just reads this map from a follower can
    return stale data (that node may be behind, or a deposed leader). A
    linearizable read must be served by a leader that has confirmed it still leads
    (heartbeat round / read-index). That check lives on the read path
    (`replication.read`), not here — this map only ever reflects applied state.

**Why there is no lock in this file.** The Rust `Store` wrapped its map in a
`Mutex` because any thread could reach it. Here, `apply` and `get` are plain
synchronous functions: they contain no `await`, so the event loop cannot suspend
them partway and run something else in between. On one loop, a function without
an `await` *is* the critical section. Adding an `asyncio.Lock` around them would
buy nothing and cost a suspension point — which is worse than useless, because
it would introduce the interleaving the lock was meant to prevent. The moment you
add an `await` to `apply` (an async snapshot write, say), that reasoning stops
holding and you owe it a fresh look. This is the one place in the project where
Python's concurrency model makes the Rust design *simpler*, not harder; `node.py`
is where it makes it harder.
"""

from __future__ import annotations

from .rpc import LogEntry, LogIndex

__all__ = ["Store"]


class Store:
    """The deterministic state machine behind the KV API."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._last_applied: LogIndex = 0
        """The index of the last log entry folded into `_data`. The invariant that
        this advances by exactly 1 per `apply` is what keeps nodes in sync."""

    @property
    def last_applied(self) -> LogIndex:
        """The highest log index this machine has applied."""
        return self._last_applied

    def apply(self, entry: LogEntry) -> str | None:
        """Apply one committed entry, returning the prior value if the command
        replaced or removed one. **The core of V3.**

        TODO(V3): dispatch on `entry.command` and advance `_last_applied` to
        `entry.index`.

          * `match entry.command:` with class patterns
            (`case SetCommand(key=k, value=v):`) is the direct Python equivalent
            of the Rust `match` — and unlike an `if/elif` chain, adding a fourth
            command later gives you one obvious place it is missing.
          * `Set` inserts, `Delete` removes, `Noop` does nothing — but `Noop`
            still advances `_last_applied`, because it is a real entry occupying a
            real index. Skipping it puts this node one index behind every other
            one forever.
          * Enforce the sequencing invariant: an entry whose index is not exactly
            `_last_applied + 1` must be refused, loudly. A gap means the caller has
            a bug, and applying anyway turns that bug into permanent divergence
            that no later repair can detect.

        **Stretch:** dedupe by a per-client request id so a retried command (Raft
        is at-least-once) isn't applied twice.
        """
        raise NotImplementedError("V3: apply this committed command and bump last_applied")

    def get(self, key: str) -> str | None:
        """Read the current value for `key` from applied state.

        Wired: this is just the dict lookup. Whether it is *safe* to serve this to
        a client (leadership confirmed, applied caught up) is decided upstream on
        the read path — see the linearizability note in the module docstring.
        """
        return self._data.get(key)

    def snapshot(self) -> bytes:
        """Serialize the whole machine into a snapshot blob (V4).

        TODO(V4): encode `_data` + `_last_applied` into bytes for
        `InstallSnapshot` and on-disk compaction. `json.dumps(...).encode()` is
        fine and is what the log's own format probably wants to match; `pickle` is
        not — a snapshot arrives over the network from another node, and
        unpickling attacker-controlled bytes is arbitrary code execution. That is
        a real decision the peer-trust-boundary checklist item asks you to state.

        Whatever you choose has to be **deterministic**: two nodes applying the
        same log must produce byte-identical snapshots, or the V3 determinism
        proof has nothing to compare. `sort_keys=True` is not decoration.
        """
        raise NotImplementedError("V4: serialize the state machine (data + last_applied)")

    def restore(self, data: bytes) -> None:
        """Replace the machine wholesale from a snapshot blob (V4) — what a
        follower does on `InstallSnapshot`.

        TODO(V4): decode `data` and overwrite `_data` + `_last_applied`. Replace,
        never merge: the snapshot is the leader's complete state at
        `last_included_index`, and anything surviving from before it is by
        definition state this node should not have.
        """
        raise NotImplementedError("V4: restore the state machine from a snapshot blob")
