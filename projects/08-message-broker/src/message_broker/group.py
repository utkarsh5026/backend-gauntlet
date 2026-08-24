"""V4 — Consumer groups & durable offset commits: at-least-once delivery.

A consumer group is a set of members that *share* one cursor per partition and
*split* the topic's partitions between them. Two things make it a group and not
just a reader:

  1. **Durable committed offsets.** For each `(group, topic, partition)` the
     coordinator stores how far the group has read, and it survives a broker
     restart — so a returning consumer resumes from the commit, not from 0.
     Different groups keep independent commits over the same topic, which is why
     adding an analytics consumer does not slow the one already running.
  2. **Assignment.** Each partition is owned by at most one member at a time; a
     member joining or leaving triggers a reassignment that keeps every partition
     covered.

Delivery is **at-least-once** because a consumer commits *after* it processes:
die in between and the next fetch re-reads from the last commit — redelivery,
never silent loss. Flip that ordering (commit first, then process) and you have
at-most-once, and a crash eats messages. Nothing else changes; the guarantee
*is* the ordering. That is a `Done when` criterion and a design-doc line.

The committed offsets are the broker's own durable state, so they live on disk
under `groups/` — the same append-only discipline as V1. (Kafka stores them in an
internal `__consumer_offsets` topic; a file per group is the learning stand-in.)

**Making a commit actually durable in Python.** `json.dump` to the real path is
the wrong move twice over: a crash mid-write leaves a truncated file that will
not parse, *and* a returning `f.write()` only reached the page cache. The
recipe:

    write a temp file in the same directory -> f.flush() -> os.fsync(f.fileno())
    -> os.replace(tmp, final) -> fsync the *directory* fd

`os.replace` is atomic on POSIX and Windows, so a reader sees the old file or the
new one and never a half-written one. The directory fsync is the step everyone
forgets: the rename itself is metadata, and it can be lost independently of the
file contents. All of it is blocking, so it belongs in `asyncio.to_thread` — a
commit is on the consumer's hot path and an unoffloaded fsync stalls every other
connection.

The in-memory view needs an `asyncio.Lock` for the same reason `Partition` does:
read-modify-write across an await is not atomic just because there is one thread.

Scaffold state: the coordinator is constructed and wired to the routes, but every
operation raises. The first join or commit is your worklist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Assignment", "GroupCoordinator", "GroupState"]


@dataclass(frozen=True, slots=True)
class Assignment:
    """The partitions a member is told to consume after a join or rebalance."""

    partitions: tuple[int, ...]
    """A tuple, not a list: an assignment handed to a member is a decision
    already made, and a caller that can append to it can invent ownership the
    coordinator never granted."""


class GroupState:
    """Per-group state: committed offsets plus who is currently in the group."""

    __slots__ = ("committed", "members")

    def __init__(self) -> None:
        self.committed: dict[tuple[str, int], int] = {}
        """`(topic, partition) -> committed offset`. The durable bookmark.

        A tuple key rather than nested dicts: it is the natural composite key,
        it is hashable for free, and it keeps "has this group ever committed
        here?" a single lookup instead of two.
        """

        self.members: list[str] = []
        """Current member ids, in join order — assignment has to be
        deterministic, and a set would make it depend on hash iteration order."""

    def __repr__(self) -> str:
        return f"GroupState(members={self.members!r}, committed={self.committed!r})"


class GroupCoordinator:
    """Owns every consumer group's durable committed offsets and live
    membership."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._groups: dict[str, GroupState] = {}
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    @classmethod
    def open(cls, directory: Path) -> GroupCoordinator:
        """Open the coordinator, creating `directory` if needed.

        Plumbing creates the directory. **Loading** the committed offsets back
        into memory is V4 recovery work.

        TODO(V4 recovery): read each group's persisted offsets under `directory`
        back into `_groups`, so a restart resumes where every group left off.
        Starting empty here means a restart currently forgets every commit —
        that regression is exactly what your restart test should catch.
        """
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory)

    async def commit(self, group: str, topic: str, partition: int, offset: int) -> None:
        """Commit the group's progress for one `(topic, partition)`.

        Must be durable: a crash *after* this returns must not lose the commit.
        An unpersisted commit that a restart forgets does not merely lose
        progress — it silently converts at-least-once into "replay from
        wherever", which is a different system.

        TODO(V4): update the in-memory map **and** persist it (see the module
        docstring's atomic-write recipe) before returning. Decide whether commits
        may go backwards: allowing it lets a consumer deliberately rewind, and
        forbidding it makes a late duplicate commit harmless. Either is
        defensible; picking neither is not.
        """
        raise NotImplementedError("V4: durably record a group's committed offset for a partition")

    async def committed(self, group: str, topic: str, partition: int) -> int | None:
        """The group's committed offset for `(topic, partition)`.

        `None` when the group has never committed there. That is genuinely
        different from `0`, and the caller decides what a fresh consumer does
        with it — start at the log beginning, or jump to the end and only see new
        records. Collapsing `None` into `0` here would take that choice away.
        """
        raise NotImplementedError("V4: look up a group's committed offset")

    async def join(self, group: str, member: str, topic: str, partition_count: int) -> Assignment:
        """A member joins the group for `topic`; returns the partitions it now
        owns.

        TODO(V4): add `member` to the group, then recompute the assignment so the
        `partition_count` partitions are split across all current members with
        each partition owned by exactly one member, and return this member's
        share. A rebalance also changes what the *other* members own — decide how
        you expose that (do they poll, or does the next fetch tell them?) and
        write it down.

        Keep the split deterministic given the member list: the same members in
        the same order must produce the same assignment, or two members will
        briefly disagree about who owns what and both consume the same partition.
        """
        raise NotImplementedError("V4: add the member and assign it a disjoint slice of partitions")

    async def leave(self, group: str, member: str) -> None:
        """A member leaves; its partitions are reassigned to the rest.

        TODO(V4): remove `member` and rebalance so no partition is left unowned
        while any member remains. Leaving is the easy half of a rebalance because
        the member told you; the hard half — a member that just stops answering —
        is the stretch goal.
        """
        raise NotImplementedError("V4: remove the member and rebalance its partitions")

    async def flush(self) -> None:
        """Durably persist every group's offsets on shutdown.

        TODO(V4): if your commit path is batched rather than write-through, this
        is where the last batch lands. Under a write-through policy it is a
        no-op, and saying which one you built is the point.
        """
        raise NotImplementedError("V4: persist any uncommitted group offsets")
