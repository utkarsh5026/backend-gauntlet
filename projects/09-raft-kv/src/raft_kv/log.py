"""The persistent replicated log — the durable spine of consensus.

Raft splits a node's state into **persistent** (must survive a crash before a
reply is sent: `current_term`, `voted_for`, and the log entries) and **volatile**
(rebuilt on restart: commit index, roles, leader bookkeeping). This module owns
the persistent part's storage: the ordered list of `LogEntry`s plus the metadata,
and the job of getting them onto disk.

Why persistence is not optional: Raft's safety proof assumes that once a node
votes in a term, or acknowledges an entry, it *remembers* that across a restart.
A node that forgets its vote can vote twice in one term and elect two leaders. So
`persist` must make the bytes durable **before** the RPC handler replies — that
ordering is V1/V2 work, wired here as a `NotImplementedError`.

## Two Python-specific hazards to get straight before you write any of it

**1. `persist` is `async` because fsync is not.** `os.fsync` blocks the calling
thread until the disk says yes — which, on the event loop, means every other
request, every heartbeat, and every peer reply waits for that disk. On a busy
leader that is how a healthy cluster starts electing new leaders: the followers
never see a heartbeat because the leader was in `fsync`. The signature is `async`
to force the fix into the design: do the write and the fsync inside
`asyncio.to_thread(...)`, so the loop keeps turning while the disk works. This is
the "no blocking call on the event loop" horizontal item, and `PYTHONASYNCIODEBUG=1`
will name this function if you get it wrong.

**2. Negative list indices silently return the wrong entry.** Entries are
**1-based** (index 0 = empty), and once snapshots (V4) compact a prefix away,
`snapshot_last_index` records where the physical `entries` list now begins — so a
logical index maps to a position by subtracting it. In Rust an underflowing
`usize` panicked or `.get()` returned `None`. In Python, `entries[-1]` is the
*last* entry and `entries[-3:]` is the *tail*: an index that has fallen below the
snapshot base does not fail, it returns plausible-looking wrong data, and the
consistency check happily matches against an entry the leader never sent. Every
position computed in this module is therefore range-checked explicitly before
indexing. Keep doing that.

The scaffold keeps the snapshot base at 0 (no snapshot yet), so the subtraction
is a no-op until V4 — which is exactly why the bug would not show up in your V1
and V2 tests.
"""

from __future__ import annotations

from pathlib import Path

from .rpc import Command, LogEntry, LogIndex, NodeId, SetCommand, Term

__all__ = ["RaftLog", "set_command"]


class RaftLog:
    """A node's persistent state: the log plus the two metadata fields Raft
    requires to be durable.

    Kept together in one object because they must be *persisted* together — a
    write that saved the new term but not the new vote is exactly the torn state
    the durability rules exist to prevent.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        """Where the persisted state lives on disk (one file per node)."""

        # --- persistent metadata ---
        self._current_term: Term = 0
        """Latest term this node has seen (starts at 0, only increases)."""

        self._voted_for: NodeId | None = None
        """Candidate this node voted for in `current_term`, if any. Reset each term."""

        # --- the log itself ---
        self._entries: list[LogEntry] = []
        """Entries with logical index > `snapshot_last_index`, in order."""

        self._snapshot_last_index: LogIndex = 0
        """The last index included in the most recent snapshot (V4). Everything at
        or below it has been compacted out of `_entries`. 0 until the first
        snapshot."""

        self._snapshot_last_term: Term = 0
        """The term of `_snapshot_last_index` — needed for the consistency check
        when `prev_log_index` falls exactly on the snapshot boundary."""

    @classmethod
    def open(cls, path: Path) -> RaftLog:
        """Load persisted state from `path`, or start empty if there is none.

        Plumbing creates the directory. **Recovery** — reading back
        `current_term`, `voted_for`, and every entry so a restarted node resumes
        exactly where it left off — is the durability half of V1/V2.

        TODO(V1/V2 durability): if a file exists at `path`, decode it and restore
        `_current_term` / `_voted_for` / `_entries` (and the snapshot base, V4).
        Recovery must stop at a **clean entry boundary**: a process killed
        mid-write leaves a truncated tail, and a half-written entry has to be
        discarded, not guessed at. Whether that is even possible depends on the
        format you pick in `persist` — decide the two together.

        Reading here is synchronous on purpose: it happens once, during startup,
        before the server accepts a connection, so there is no loop to block yet.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(path)

    async def persist(self) -> None:
        """Durably write the persistent state (term, vote, log) so it survives a
        crash. Must complete **before** the caller replies to an RPC.

        TODO(V1/V2): serialize the persistent fields and get them onto the disk —
        write, then `os.fsync`, then (if you are doing write-to-temp) `os.replace`
        for an atomic swap. An append-only record file is the other reasonable
        shape; whichever you choose, write down how a torn tail is detected on the
        way back in, because `open` has to agree with it.

        Do the file work in `asyncio.to_thread` — see hazard 1 in the module
        docstring. And note the fsync-per-write throughput cost is the same dial
        as project 08's log: batching several appends into one fsync trades a
        wider crash window for real throughput, and that trade belongs in
        `docs/09-design.md`.
        """
        raise NotImplementedError(
            "V1/V2: durably persist current_term, voted_for and the log entries"
        )

    # ---- persistent metadata accessors (wired — trivial) --------------------

    @property
    def current_term(self) -> Term:
        return self._current_term

    def set_current_term(self, term: Term) -> None:
        """Set the current term and clear the vote (a new term means a fresh
        ballot). The caller must `await persist()` before acting on it."""
        self._current_term = term
        self._voted_for = None

    @property
    def voted_for(self) -> NodeId | None:
        return self._voted_for

    def set_voted_for(self, candidate: NodeId | None) -> None:
        self._voted_for = candidate

    # ---- log geometry (wired — the index math the consensus code leans on) ---

    @property
    def last_index(self) -> LogIndex:
        """The index of the last entry (the snapshot base if nothing is retained)."""
        return self._entries[-1].index if self._entries else self._snapshot_last_index

    @property
    def last_term(self) -> Term:
        """The term of the last entry — half of the "up-to-date" comparison a
        voter makes in V1."""
        return self._entries[-1].term if self._entries else self._snapshot_last_term

    def term_at(self, index: LogIndex) -> Term | None:
        """The term of the entry at `index`, if the log holds it.

        `None` if `index` is beyond the tail or has been compacted away; the
        snapshot boundary answers for `snapshot_last_index` itself, which is what
        keeps the consistency check working across the seam.
        """
        if index == self._snapshot_last_index and index != 0:
            return self._snapshot_last_term
        entry = self.get(index)
        return entry.term if entry is not None else None

    def get(self, index: LogIndex) -> LogEntry | None:
        """The entry at logical `index`, if it is still physically retained.

        The `0 <= pos` guard is load-bearing, not defensive noise: without it a
        compacted-away index becomes a negative position and Python hands back an
        entry from the *end* of the log. See hazard 2 in the module docstring.
        """
        pos = index - self._snapshot_last_index - 1
        if pos < 0 or pos >= len(self._entries):
            return None
        return self._entries[pos]

    def entries_from(self, start: LogIndex) -> list[LogEntry]:
        """Every retained entry at index >= `start` — what the leader ships in an
        `AppendEntries` to a follower that needs them (V2).

        `start` is clamped up to the first retained index, so a slice can never be
        taken from a negative position (which would silently return a tail).
        """
        clamped = max(start, self._snapshot_last_index + 1)
        pos = clamped - self._snapshot_last_index - 1
        return self._entries[pos:]

    def append(self, entries: list[LogEntry]) -> None:
        """Append entries to the tail.

        Wired mechanics; *when* it is safe to call, and how conflicts are resolved
        before it, is the V2 logic in `replication.py`.
        """
        self._entries.extend(entries)

    def truncate_from(self, start: LogIndex) -> None:
        """Drop every entry at index >= `start` — used when a follower's tail
        conflicts with the leader's and must be overwritten (V2).

        Committed entries must never reach here; that invariant is the caller's to
        uphold, and it is the difference between repairing a log and losing an
        acknowledged write.
        """
        pos = start - self._snapshot_last_index - 1
        if pos <= 0:
            self._entries.clear()
            return
        del self._entries[pos:]

    # ---- the snapshot seam (V4) ---------------------------------------------

    @property
    def snapshot_point(self) -> tuple[LogIndex, Term]:
        """The last index the state machine has folded into a snapshot, and its
        term."""
        return self._snapshot_last_index, self._snapshot_last_term

    def compact_to(self, last_index: LogIndex, last_term: Term) -> None:
        """Discard every entry at or below `last_index` and move the snapshot base
        there (V4).

        Wired mechanics, deliberately: the mechanics are index arithmetic, and the
        *decisions* — that `last_index` may never exceed `last_applied`, and that
        the snapshot must be durable before this is called — are yours to make in
        `snapshot.py`. This function will happily discard an uncommitted entry if
        you ask it to.
        """
        pos = last_index - self._snapshot_last_index
        if pos > 0:
            del self._entries[:pos]
        self._snapshot_last_index = last_index
        self._snapshot_last_term = last_term

    def reset_to_snapshot(self, last_index: LogIndex, last_term: Term) -> None:
        """Throw the log away entirely and re-base it at a snapshot boundary — what
        a follower does when it accepts an `InstallSnapshot` (V4)."""
        self._entries.clear()
        self._snapshot_last_index = last_index
        self._snapshot_last_term = last_term

    def __len__(self) -> int:
        """Number of entries physically retained (post-compaction) — the signal the
        snapshot trigger (V4) watches. `len(log)` reads better than `log.len()`."""
        return len(self._entries)


def set_command(key: str, value: str) -> Command:
    """Build a `Set` command — small convenience the client-write path uses."""
    return SetCommand(key=key, value=value)
